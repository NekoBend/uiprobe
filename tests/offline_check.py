"""Opt-in check that the test process has no route out of the machine.

With ``UIPROBE_TEST_REQUIRE_OFFLINE=1`` the test session refuses to start
unless every route of the current network namespace goes through the loopback
interface, as it does inside ``unshare -rn``. The files in ``/proc/net`` show
the namespace of the reading process.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

VARIABLE = "UIPROBE_TEST_REQUIRE_OFFLINE"
LOOPBACK = "lo"
IPV4_ROUTES = Path("/proc/net/route")
IPV6_ROUTES = Path("/proc/net/ipv6_route")


def non_loopback_ipv4(text: str) -> list[str]:
    """Return the interfaces of the IPv4 routes in `text` that are not ``lo``.

    The first line is a header; the interface is the first column.
    """
    names = (line.split(maxsplit=1) for line in text.splitlines()[1:])
    return [parts[0] for parts in names if parts and parts[0] != LOOPBACK]


def non_loopback_ipv6(text: str) -> list[str]:
    """Return the interfaces of the IPv6 routes in `text` that are not ``lo``.

    There is no header; the interface is the last of ten columns.
    """
    names = (line.split() for line in text.splitlines())
    return [parts[-1] for parts in names if parts and parts[-1] != LOOPBACK]


def _read_routes(path: Path, *, required: bool) -> str:
    try:
        return path.read_text(encoding="ascii")
    except FileNotFoundError:
        # A kernel without IPv6 has no ipv6_route file, and so no IPv6 route.
        if required:
            raise
        return ""


def offline_violation(environ: Mapping[str, str]) -> str | None:
    """Return why the session must not start, or None when it may.

    None also means the check was not asked for.
    """
    if environ.get(VARIABLE) != "1":
        return None
    try:
        outside = non_loopback_ipv4(_read_routes(IPV4_ROUTES, required=True))
        outside += non_loopback_ipv6(_read_routes(IPV6_ROUTES, required=False))
    except OSError as exc:
        return f"{VARIABLE}=1 but the network routes cannot be read: {exc}"
    if not outside:
        return None
    return (
        f"{VARIABLE}=1 but this network namespace has routes through "
        f"{', '.join(sorted(set(outside)))}. Run the tests inside "
        "`unshare -rn` (a namespace with no route out)."
    )
