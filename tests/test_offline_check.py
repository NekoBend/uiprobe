"""Tests of the opt-in offline enforcement (tests/offline_check.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests import offline_check
from tests.conftest import pytest_sessionstart
from tests.offline_check import (
    VARIABLE,
    non_loopback_ipv4,
    non_loopback_ipv6,
    offline_violation,
)

IPV4_HEADER = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\t"
    "MTU\tWindow\tIRTT"
)
IPV4_DEFAULT = "eth0\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0"
IPV4_LOCAL = "eth0\t0002A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0"
IPV4_LOOPBACK = "lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0"
IPV6_LOOPBACK = (
    "00000000000000000000000000000001 80 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 00000000 00000002 00000000 80200001 lo"
)
IPV6_DEFAULT = (
    "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
    "fe800000000000000000000000000001 00000400 00000001 00000000 00000003 eth0"
)

if TYPE_CHECKING:
    from pathlib import Path


def table(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_an_empty_ipv4_table_has_no_route_out() -> None:
    assert non_loopback_ipv4(table(IPV4_HEADER)) == []
    assert non_loopback_ipv4("") == []


def test_ipv4_loopback_routes_are_allowed() -> None:
    assert non_loopback_ipv4(table(IPV4_HEADER, IPV4_LOOPBACK)) == []


def test_ipv4_default_and_local_routes_are_found() -> None:
    text = table(IPV4_HEADER, IPV4_DEFAULT, IPV4_LOCAL, IPV4_LOOPBACK)
    assert non_loopback_ipv4(text) == ["eth0", "eth0"]


def test_ipv6_loopback_routes_are_allowed() -> None:
    assert non_loopback_ipv6(table(IPV6_LOOPBACK)) == []
    assert non_loopback_ipv6("") == []


def test_ipv6_default_route_is_found() -> None:
    assert non_loopback_ipv6(table(IPV6_LOOPBACK, IPV6_DEFAULT)) == ["eth0"]


@pytest.fixture
def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    ipv4, ipv6 = tmp_path / "route", tmp_path / "ipv6_route"
    monkeypatch.setattr(offline_check, "IPV4_ROUTES", ipv4)
    monkeypatch.setattr(offline_check, "IPV6_ROUTES", ipv6)
    return ipv4, ipv6


def test_nothing_is_checked_unless_asked_for(routes: tuple[Path, Path]) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_DEFAULT))
    assert offline_violation({}) is None
    assert offline_violation({VARIABLE: "0"}) is None
    assert offline_violation({VARIABLE: ""}) is None


def test_a_namespace_with_only_loopback_passes(routes: tuple[Path, Path]) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_LOOPBACK))
    routes[1].write_text(table(IPV6_LOOPBACK))
    assert offline_violation({VARIABLE: "1"}) is None


def test_a_route_out_names_the_interface(routes: tuple[Path, Path]) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_DEFAULT))
    routes[1].write_text(table(IPV6_LOOPBACK))
    message = offline_violation({VARIABLE: "1"})
    assert message is not None
    assert "eth0" in message
    assert "unshare -rn" in message


def test_an_ipv6_route_out_is_a_violation(routes: tuple[Path, Path]) -> None:
    routes[0].write_text(table(IPV4_HEADER))
    routes[1].write_text(table(IPV6_DEFAULT))
    assert "eth0" in (offline_violation({VARIABLE: "1"}) or "")


def test_a_kernel_without_ipv6_is_not_an_error(routes: tuple[Path, Path]) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_LOOPBACK))
    assert offline_violation({VARIABLE: "1"}) is None


def test_unreadable_ipv4_routes_fail_the_check(routes: tuple[Path, Path]) -> None:
    del routes
    message = offline_violation({VARIABLE: "1"})
    assert message is not None
    assert "cannot be read" in message


def test_the_session_hook_exits_when_a_route_leads_out(
    routes: tuple[Path, Path],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_DEFAULT))
    monkeypatch.setenv(VARIABLE, "1")
    with pytest.raises(pytest.exit.Exception, match="eth0"):
        pytest_sessionstart(request.session)


def test_the_session_hook_does_nothing_when_not_asked(
    routes: tuple[Path, Path],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes[0].write_text(table(IPV4_HEADER, IPV4_DEFAULT))
    monkeypatch.delenv(VARIABLE, raising=False)
    pytest_sessionstart(request.session)
