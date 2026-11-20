"""Fail every request of a short crawl once, in turn, and check the pin rule.

The rule: after a request on the pinned device failed, no input, force-stop
or app start goes out until the identity was read again, successfully,
through the number that input goes to; once the pin failed, nothing goes
out at all. Each run fails exactly one adb client call (the N-th) with one
kind of error, for every N of the run without faults and every kind.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import FakeAdbServer
from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, TRANSPORT, Clock, run
from uiprobe.adb import AdbClient
from uiprobe.crawl import Limits
from uiprobe.device import IDENTITY_COMMAND, Device
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    UiprobeError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

KINDS: dict[str, Callable[[], UiprobeError]] = {
    "offline": lambda: DeviceOfflineError("device offline", details={}),
    "not_found": lambda: DeviceNotFoundError(
        f"device {SERIAL!r} not found", details={"serial": SERIAL}
    ),
    "server_gone": lambda: AdbServerUnavailableError(
        "adb server is unavailable", details={}
    ),
    "broken": lambda: AdbError("connection closed", details={}),
}
CHANGING = ("input ", "monkey ", "am ")
LIMITS = Limits(actions=4)


class Faults:
    """Counts adb client calls, fails the `fail_at`-th, logs every call."""

    def __init__(self, fail_at: int | None, kind: str) -> None:
        self.calls = 0
        self.fail_at = fail_at
        self.kind = kind
        self.failed_at: int | None = None  # index in `log`
        # (outcome, method, command, transport id, the pin had failed)
        self.log: list[tuple[str, str, str | None, int | None, bool]] = []
        self.device: Device | None = None

    def wrap(self, name: str, original: Callable[..., object]) -> Callable[..., object]:
        def call(client: AdbClient, *args: object, **kwargs: object) -> object:
            self.calls += 1
            command = str(args[1]) if name in ("shell", "exec_out") else None
            transport = kwargs.get("transport_id")
            number = transport if isinstance(transport, int) else None
            broken = getattr(self.device, "_broken", None) is not None
            if self.calls == self.fail_at:
                self.failed_at = len(self.log)
                self.log.append(("failed", name, command, number, broken))
                error = KINDS[self.kind]()
                raise error
            result = original(client, *args, **kwargs)
            self.log.append(("ok", name, command, number, broken))
            return result

        return call


def crawl_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, faults: Faults, name: str
) -> None:
    with FakeAdbServer() as server, monkeypatch.context() as patch:
        FakeAndroid(server, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)
        for method in ("devices", "shell", "exec_out"):
            patch.setattr(
                AdbClient, method, faults.wrap(method, getattr(AdbClient, method))
            )
        try:
            device = Device(adb_server=server.address, pin=True)
        except UiprobeError:
            return  # the fault hit the choice or the first pin: nothing else
        faults.device = device
        ticks = [0.0]

        def clock() -> float:
            ticks[0] += 0.25
            return ticks[0]

        device._clock = clock  # the test seam of Device: no real waiting
        device._sleep = lambda _seconds: None
        # An ending with an error is allowed; what went out is checked.
        with contextlib.suppress(UiprobeError):
            run(device, Clock(), tmp_path / name, limits=LIMITS)


def violations(faults: Faults) -> list[str]:
    """Inputs that went out after the fault without a fresh proof, and
    anything that went out after the pin failed."""
    found: list[str] = []
    start = faults.failed_at
    log = faults.log
    for index, (outcome, name, command, number, broken) in enumerate(log):
        if outcome != "ok":
            continue
        if broken:
            found.append(f"#{index} {name} {command!r} after the pin failed")
        if start is None or index < start or name != "shell" or command is None:
            continue
        if not command.startswith(CHANGING):
            continue
        proven = any(
            log[j][0] == "ok"
            and log[j][1] == "shell"
            and log[j][2] == IDENTITY_COMMAND
            and log[j][3] == number
            for j in range(start + 1, index)
        )
        if not proven:
            found.append(f"#{index} {command!r} on {number} without a new proof")
    return found


def test_the_crawl_without_faults_sends_inputs(tmp_path, monkeypatch):
    faults = Faults(None, "offline")
    crawl_with(tmp_path, monkeypatch, faults, "clean")
    inputs = [e for e in faults.log if e[2] and e[2].startswith(CHANGING)]
    assert len(inputs) >= 3  # a launch and taps: the faults have things to hit
    assert violations(faults) == []


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_request_failed_once_keeps_the_pin_rule(tmp_path, monkeypatch, kind):
    clean = Faults(None, kind)
    crawl_with(tmp_path, monkeypatch, clean, f"clean-{kind}")
    total = clean.calls
    assert total > 50
    broken_runs: dict[int, list[str]] = {}
    for fail_at in range(1, total + 1):
        faults = Faults(fail_at, kind)
        crawl_with(tmp_path, monkeypatch, faults, f"{kind}-{fail_at}")
        assert faults.failed_at is not None
        found = violations(faults)
        if found:
            broken_runs[fail_at] = found
    assert broken_runs == {}, f"{kind}: {len(broken_runs)} of {total} positions"
