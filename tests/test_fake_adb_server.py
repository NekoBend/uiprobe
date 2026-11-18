"""Tests of the fake adb server itself (raw protocol, no uiprobe code)."""

from __future__ import annotations

import contextlib
import re
import socket
import threading
import time

import pytest

from tests.fake_adb_server import (
    Fail,
    FakeAdbServer,
    Reset,
    Stall,
    Trickle,
    frame,
    render_devices,
)


def talk(server: FakeAdbServer, *requests: str) -> bytes:
    """Send requests on one connection and return everything until EOF."""
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        for request in requests:
            payload = request.encode()
            sock.sendall(f"{len(payload):04x}".encode() + payload)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    return data


def test_address_and_context_manager_cleanup() -> None:
    before = threading.active_count()
    with FakeAdbServer() as server:
        assert re.fullmatch(r"tcp:127\.0\.0\.1:\d+", server.address)
        talk(server, "host:version")
    assert threading.active_count() == before


def test_version(fake_adb: FakeAdbServer) -> None:
    assert talk(fake_adb, "host:version") == b"OKAY" + frame(b"0029")
    fake_adb.version = 0x1234
    assert talk(fake_adb, "host:version") == b"OKAY" + frame(b"1234")


def test_devices_rendering(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("emu-1", model="Pixel", transport_id=3)
    fake_adb.add_device("usb-1", "unauthorized")
    reply = talk(fake_adb, "host:devices-l")
    body = render_devices(fake_adb.devices).encode()
    assert reply == b"OKAY" + frame(body)
    assert b"model:Pixel transport_id:3" in body
    assert b"usb-1" in body


def test_shell_and_exec_scripts(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "echo hi", b"hi\n")
    fake_adb.on_shell("d1", re.compile(r"^upper "), lambda c: c.upper().encode())
    fake_adb.on_exec("d1", "cat x", b"\x00\xff")
    fake_adb.on_shell("d1", "boom", Fail("nope"))
    assert talk(fake_adb, "host:transport:d1", "shell:echo hi") == b"OKAYOKAYhi\n"
    assert talk(fake_adb, "host:transport:d1", "shell:upper a").endswith(b"UPPER A")
    assert talk(fake_adb, "host:transport:d1", "exec:cat x").endswith(b"\x00\xff")
    assert talk(fake_adb, "host:transport:d1", "shell:boom") == (
        b"OKAY" + b"FAIL" + frame(b"nope")
    )
    assert talk(fake_adb, "host:transport:d1", "shell:unscripted") == b"OKAYOKAY"


def test_transport_failures(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("off", "offline")
    fake_adb.add_device("unauth", "unauthorized")
    assert b"not found" in talk(fake_adb, "host:transport:none")
    assert b"offline" in talk(fake_adb, "host:transport:off")
    assert b"unauthorized" in talk(fake_adb, "host:transport:unauth")


def test_service_without_transport_and_unknown_requests(
    fake_adb: FakeAdbServer,
) -> None:
    assert talk(fake_adb, "shell:id").startswith(b"FAIL")
    assert talk(fake_adb, "host:bogus").startswith(b"FAIL")


def test_scripted_fail_raw_and_stall(fake_adb: FakeAdbServer) -> None:
    fake_adb.fail_request("host:version", "no version")
    fake_adb.send_raw("host:devices-l", b"OKAY00")
    assert talk(fake_adb, "host:version") == b"FAIL" + frame(b"no version")
    assert talk(fake_adb, "host:devices-l") == b"OKAY00"
    assert fake_adb.requests == ["host:version", "host:devices-l"]


def test_stall_is_released_on_stop() -> None:
    server = FakeAdbServer()
    server.stall("host:version")
    server.start()
    result: list[bytes] = []
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    sock.sendall(b"000chost:version")
    thread = threading.Thread(target=lambda: result.append(sock.recv(10)))
    thread.start()
    # Stopping before the request is accepted would reset the connection.
    deadline = time.monotonic() + 5
    while server.requests != ["host:version"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert server.requests == ["host:version"]
    server.stop()
    thread.join()
    sock.close()
    assert result == [b""]


def send_raw_header(server: FakeAdbServer, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(data)
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
    return reply


@pytest.mark.parametrize(
    "header", [b"0x0c", b" 00c", b"+00c", b"0_0c", b"zzzz", b"-00c", b"00c\n"]
)
def test_malformed_length_headers_are_protocol_errors(
    header: bytes,
) -> None:
    # `int(header, 16)` would accept several of these; a real server does not.
    with FakeAdbServer() as server:
        reply = send_raw_header(server, header)
        assert reply == b"FAIL" + frame(b"protocol fault")
        assert server.requests == []
        assert len(server.protocol_errors) == 1
        assert server.handler_errors == []


def test_uppercase_hex_headers_are_accepted() -> None:
    with FakeAdbServer() as server:
        assert send_raw_header(server, b"000Chost:version").startswith(b"OKAY")
        assert server.protocol_errors == []


def test_non_utf8_payload_is_a_protocol_error() -> None:
    with FakeAdbServer() as server:
        send_raw_header(server, b"0002\xff\xfe")
        assert len(server.protocol_errors) == 1


def test_wire_records_the_bytes_received(fake_adb: FakeAdbServer) -> None:
    talk(fake_adb, "host:version")
    assert fake_adb.wire == [b"000chost:version"]


def test_a_crashing_handler_is_recorded_not_printed(
    fake_adb: FakeAdbServer, capfd: pytest.CaptureFixture[str]
) -> None:
    def explode(_command: str) -> bytes:
        msg = "boom"
        raise RuntimeError(msg)

    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "x", explode)
    talk(fake_adb, "host:transport:d1", "shell:x")
    assert fake_adb.handler_errors == ["RuntimeError('boom')"]
    assert capfd.readouterr().err == ""
    fake_adb.handler_errors.clear()


def test_stall_prefix_trickle_and_slow_responses(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "t", Trickle(b"abcdef", chunk_size=2))
    assert talk(fake_adb, "host:transport:d1", "shell:t").endswith(b"OKAYabcdef")


def test_okay_waits_for_a_slow_callable(fake_adb: FakeAdbServer) -> None:
    """A callable is evaluated before OKAY; silence after OKAY is `Stall`."""
    release = threading.Event()
    entered = threading.Event()

    def slow(_command: str) -> bytes:
        entered.set()
        assert release.wait(30)
        return b"late"

    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", slow)
    with socket.create_connection(("127.0.0.1", fake_adb.port), timeout=5) as sock:
        try:
            for request in ("host:transport:d1", "shell:slow"):
                payload = request.encode()
                sock.sendall(f"{len(payload):04x}".encode() + payload)
            assert sock.recv(4) == b"OKAY"  # the transport
            # Wait until the handler runs, so the silence below cannot be a
            # handler that has merely not started yet.
            assert entered.wait(10)
            sock.settimeout(0.05)
            with pytest.raises(TimeoutError):
                sock.recv(4)  # the callable is blocked, so no OKAY yet
        finally:
            release.set()
        sock.settimeout(5)
        assert sock.recv(4) == b"OKAY"
        assert sock.recv(4) == b"late"


def test_chunked_writes_deliver_the_same_bytes(fake_adb: FakeAdbServer) -> None:
    fake_adb.chunk_size = 1
    assert talk(fake_adb, "host:version") == b"OKAY" + frame(b"0029")


def read_until_eof(sock: socket.socket) -> bytes:
    data = b""
    while chunk := sock.recv(4096):
        data += chunk
    return data


def test_reset_closes_with_rst(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.reset("shell:id")
    with socket.create_connection(("127.0.0.1", fake_adb.port), timeout=5) as sock:
        for request in ("host:transport:d1", "shell:id"):
            payload = request.encode()
            sock.sendall(f"{len(payload):04x}".encode() + payload)
        with pytest.raises(ConnectionResetError):
            read_until_eof(sock)


def test_callable_may_return_fail_stall_or_trickle_before_okay(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", re.compile(r"^fail"), lambda _: Fail("no"))
    fake_adb.on_shell("d1", re.compile(r"^slow"), lambda _: Trickle(b"abc", 1, 0.0))
    # FAIL replaces the OKAY of the service (the OKAY seen is the transport's).
    assert talk(fake_adb, "host:transport:d1", "shell:fail") == (
        b"OKAY" + b"FAIL" + frame(b"no")
    )
    assert talk(fake_adb, "host:transport:d1", "shell:slow") == b"OKAYOKAYabc"


def test_callable_stall_goes_silent_after_the_prefix(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "hang", lambda _: Stall(b"part"))
    with socket.create_connection(("127.0.0.1", fake_adb.port), timeout=5) as sock:
        for request in ("host:transport:d1", "shell:hang"):
            sock.sendall(f"{len(request):04x}{request}".encode())
        data = b""
        while len(data) < 12:
            chunk = sock.recv(64)
            assert chunk, data  # the server must not close early
            data += chunk
        assert data == b"OKAYOKAYpart"
        sock.settimeout(0.2)
        with pytest.raises(TimeoutError):
            sock.recv(1)


def test_commands_are_recorded_in_order(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    talk(fake_adb, "host:transport:d1", "shell:one")
    talk(fake_adb, "host:transport:d1", "exec:two")
    assert fake_adb.commands == [("d1", "shell", "one"), ("d1", "exec", "two")]


def test_strict_mode_records_and_refuses_unscripted_commands() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.on_shell("d1", "ok", b"fine")
        server.allow_only(["ok", re.compile(r"^cat \S+$")])
        assert talk(server, "host:transport:d1", "shell:ok").endswith(b"fine")
        assert talk(server, "host:transport:d1", "exec:cat x") == b"OKAYOKAY"
        refused = talk(server, "host:transport:d1", "shell:rm -rf x")
        assert refused.startswith(b"OKAYFAIL")
        assert server.unexpected == ["shell:rm -rf x"]
        # Without strict mode, an unscripted command is an empty OKAY.
    with FakeAdbServer() as lax:
        lax.add_device("d1")
        assert talk(lax, "host:transport:d1", "shell:anything") == b"OKAYOKAY"
        assert lax.unexpected == []


def test_scripted_requests_are_recorded_and_checked_by_strict_mode() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.allow_only(["ok"])
        server.reset("shell:ok")
        server.fail_request("shell:rm -rf x", "no")
        with contextlib.suppress(OSError):  # the reset may surface as an error
            talk(server, "host:transport:d1", "shell:ok")
        talk(server, "host:transport:d1", "shell:rm -rf x")
        assert server.commands == [("d1", "shell", "ok"), ("d1", "shell", "rm -rf x")]
        assert server.unexpected == ["shell:rm -rf x"]


@pytest.mark.parametrize("script", ["reset", "stall", "send_raw", "fail_request"])
def test_strict_mode_judges_a_scripted_shell_command_first(script) -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.allow_only(["ok"])
        args = {"send_raw": (b"OKAY",), "fail_request": ("scripted",)}.get(script, ())
        getattr(server, script)("shell:rm -rf x", *args)
        with contextlib.suppress(OSError):
            reply = talk(server, "host:transport:d1", "shell:rm -rf x")
            assert reply.startswith(b"OKAYFAIL")
        assert server.unexpected == ["shell:rm -rf x"]


def test_strict_mode_keeps_a_script_on_an_admitted_command() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.allow_only(["ok"])
        server.fail_request("shell:ok", "scripted")
        reply = talk(server, "host:transport:d1", "shell:ok")
        assert reply == b"OKAYFAIL" + b"0008scripted"
        assert server.unexpected == []


def test_strict_mode_records_a_scripted_command_without_a_transport() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.allow_only(["hello"])
        server.fail_request("shell:hello", "scripted")
        talk(server, "shell:hello")
        assert server.unexpected == ["shell:hello"]


def test_strict_mode_records_unknown_services() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        talk(server, "host:nonsense")  # not strict: nothing recorded
        assert server.unexpected == []
        server.allow_only([])
        talk(server, "host:nonsense")
        talk(server, "sync:")
        assert server.unexpected == ["host service host:nonsense", "request sync:"]


def test_callable_may_reset_the_connection() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        seen = []
        server.on_shell("d1", "boom", lambda c: (seen.append(c), Reset())[1])
        with contextlib.suppress(OSError):  # the reset may surface as an error
            assert talk(server, "host:transport:d1", "shell:boom") in (b"OKAY", b"")
        assert seen == ["boom"]


def test_a_script_behind_a_catch_all_is_refused_unless_first() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.on_shell("d1", re.compile(""), b"all")
        with pytest.raises(ValueError, match="shadows"):
            server.on_shell("d1", "x", b"x")
        server.on_shell("d1", "x", b"x", first=True)
        assert talk(server, "host:transport:d1", "shell:x").endswith(b"x")


def test_strict_mode_is_scoped_to_a_serial() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.add_device("d2")
        server.allow_only(["ok"], serial="d1")
        assert talk(server, "host:transport:d1", "shell:ok") == b"OKAYOKAY"
        assert server.unexpected == []
        reply = talk(server, "host:transport:d2", "shell:ok")
        assert reply.startswith(b"OKAYFAIL")
        assert server.unexpected == ["shell:ok (no allowlist for d2)"]
        # A list of its own admits the second device; the first is unchanged.
        server.unexpected.clear()
        server.allow_only(["other"], serial="d2")
        assert talk(server, "host:transport:d2", "shell:other") == b"OKAYOKAY"
        assert talk(server, "host:transport:d1", "shell:ok") == b"OKAYOKAY"
        assert server.unexpected == []
        talk(server, "host:transport:d1", "shell:other")
        assert server.unexpected == ["shell:other"]
        server.unexpected.clear()


def test_strict_mode_is_scoped_to_a_service_kind() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        server.allow_only({"exec": ["cat x"], "shell": ["ls"]})
        assert talk(server, "host:transport:d1", "exec:cat x") == b"OKAYOKAY"
        assert talk(server, "host:transport:d1", "shell:ls") == b"OKAYOKAY"
        assert server.unexpected == []
        talk(server, "host:transport:d1", "shell:cat x")
        talk(server, "host:transport:d1", "exec:ls")
        assert server.unexpected == ["shell:cat x", "exec:ls"]
        server.unexpected.clear()
    with FakeAdbServer() as plain:
        plain.add_device("d1")
        plain.allow_only(["ls"], kinds=("shell",))
        assert talk(plain, "host:transport:d1", "shell:ls") == b"OKAYOKAY"
        talk(plain, "host:transport:d1", "exec:ls")
        assert plain.unexpected == ["exec:ls"]
        plain.unexpected.clear()


def test_allow_only_refuses_an_unknown_service_kind() -> None:
    with FakeAdbServer() as server, pytest.raises(ValueError, match="service kind"):
        server.allow_only({"sync": ["x"]})


def test_admitted_is_the_position_of_this_threads_command() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        seen: list[int] = []

        def answer(command: str) -> bytes:
            del command
            seen.append(server.admitted)
            return b""

        server.on_shell("d1", re.compile(""), answer)
        talk(server, "host:transport:d1", "shell:a")
        talk(server, "host:transport:d1", "shell:b")
        assert seen == [1, 2]
