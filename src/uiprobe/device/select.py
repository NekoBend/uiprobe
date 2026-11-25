"""Choosing the device: `select_device`, `devices`, `device_details`, the identity
read, and what the pinned calls share."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NamedTuple

from uiprobe.adb import (
    AdbClient,
    DeviceInfo,
    ascii_int,
    find_adb,
    quote_args,
    resolve_server_address,
    start_blocked_by_environment,
)
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    MultipleDevicesError,
    UiprobeError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


READY_STATE: Final = "device"


OFFLINE_STATE: Final = "offline"


# The boot id is a random UUID the kernel makes at every boot, so it tells two
# emulators of one image (same serial number and build) apart, and one
# emulator before and after a restart. ``echo "$(cat ...)"`` prints exactly one
# line, empty when the file cannot be read.
IDENTITY_COMMAND: Final = (
    "getprop ro.kernel.qemu; getprop ro.serialno; getprop ro.build.fingerprint;"
    ' echo "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"'
)


_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


# details["reason"] when a request that changes the device was not sent
# because the device has to be proven again first (the pin itself is kept).
PIN_NOT_PROVEN: Final = "pin_not_proven"


class _Dispatch(NamedTuple):
    """What the crawl hands one call that changes the device: the value of
    `_monotonic` by which its requests must go out, and the `pin_epoch` it
    approved (None: none given)."""

    send_by: float | None
    epoch: int | None


def _dispatch(send_by: object, epoch: object) -> _Dispatch | None:
    """Check the private `_send_by` and `_epoch` arguments; they must be a
    built-in float and int (the crawl's own values), or None."""
    if send_by is None and epoch is None:
        return None
    if send_by is not None and type(send_by) is not float:
        msg = f"_send_by must be a float, not {type(send_by).__name__}"
        raise TypeError(msg)
    if epoch is not None and type(epoch) is not int:
        msg = f"_epoch must be an int, not {type(epoch).__name__}"
        raise TypeError(msg)
    return _Dispatch(send_by, epoch)


class DeviceIdentity(NamedTuple):
    """What `Device.identity` read: emulator or not, serial number, build and
    boot id.

    ``boot_id`` is the kernel's random boot id (a lowercase UUID), or ``""``
    when it could not be read.
    """

    emulator: bool
    serialno: str
    fingerprint: str
    boot_id: str = ""


# Failures that leave optional metadata unknown instead of failing the call.
_OPTIONAL_ERRORS: Final = (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)


def _brief(candidates: Sequence[DeviceInfo]) -> list[dict[str, object]]:
    return [{"serial": d.serial, "state": d.state} for d in candidates]


def _state_error(
    state: str, message: str, details: Mapping[str, object]
) -> DeviceUnauthorizedError | DeviceOfflineError:
    if state == "unauthorized":
        return DeviceUnauthorizedError(message, details=details)
    return DeviceOfflineError(message, details=details)


def _check_serial(serial: object) -> None:
    if serial is not None and not isinstance(serial, str):
        msg = f"serial must be a string, not {type(serial).__name__}"
        raise TypeError(msg)


def _select_requested(candidates: Sequence[DeviceInfo], serial: str) -> DeviceInfo:
    for candidate in candidates:
        if candidate.serial != serial:
            continue
        if candidate.state == READY_STATE:
            return candidate
        raise _state_error(
            candidate.state,
            f"device {serial!r} is {candidate.state}",
            {"serial": serial, "state": candidate.state},
        )
    raise DeviceNotFoundError(
        f"device {serial!r} is not connected",
        details={"serial": serial, "devices": _brief(candidates)},
    )


def select_device(
    candidates: Sequence[DeviceInfo],
    serial: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DeviceInfo:
    """Choose the device to use from the adb device list.

    The requested serial is `serial`, else ``ANDROID_SERIAL`` (empty counts as
    unset). An empty `serial` argument is a request for a device named ``""``,
    which is not connected. Without a requested serial, only devices in the
    ``device`` state count.

    Raises:
        TypeError: `serial` is not a string.
        DeviceNotFoundError: The requested device is absent, or none is connected.
        MultipleDevicesError: More than one usable device and none requested.
        DeviceUnauthorizedError: The only candidates wait for authorization.
        DeviceOfflineError: The only candidates are in another unusable state.
    """
    env = os.environ if environ is None else environ
    _check_serial(serial)
    requested = serial if serial is not None else env.get("ANDROID_SERIAL") or None
    if requested is not None:
        return _select_requested(candidates, requested)
    ready = [c for c in candidates if c.state == READY_STATE]
    if len(ready) == 1:
        return ready[0]
    if len(ready) > 1:
        serials = [c.serial for c in ready]
        raise MultipleDevicesError(
            f"{len(ready)} devices are connected; choose one with --serial",
            details={"serials": serials},
        )
    if not candidates:
        raise DeviceNotFoundError("no device is connected", details={"devices": []})
    states = {c.state for c in candidates}
    raise _state_error(
        "unauthorized" if "unauthorized" in states else "",
        "no device is ready: "
        + ", ".join(f"{c.serial} is {c.state}" for c in candidates),
        {"devices": _brief(candidates)},
    )


def _make_client(
    adb_server: str | None, adb: str | os.PathLike[str] | None, *, start_server: bool
) -> AdbClient:
    """Build the client; look for the adb executable only if it can be used.

    adb is run only to start a server nobody chose an address for, so a stale
    ``UIPROBE_ADB`` must not break a command that never needs it. An adb path
    given as an argument is still checked, to catch typos.

    Wrong argument types raise `TypeError` here, before any connection.
    """
    if not isinstance(start_server, bool):
        msg = f"start_server must be a bool, not {type(start_server).__name__}"
        raise TypeError(msg)
    address = resolve_server_address(adb_server)
    may_start = (
        start_server and address.is_default and not start_blocked_by_environment()
    )
    adb_path = find_adb(adb) if may_start or adb is not None else None
    return AdbClient(address, adb_path=adb_path, start_server=start_server)


def devices(
    *,
    adb_server: str | None = None,
    adb: str | os.PathLike[str] | None = None,
    start_server: bool = True,
) -> list[DeviceInfo]:
    """List the devices the adb server knows, in every state."""
    return _make_client(adb_server, adb, start_server=start_server).devices()


@dataclass(frozen=True, slots=True)
class DeviceDetails:
    """A listed device with what could be learned about it.

    `api_level` and `emulator` are None for a device that is not ready and for a
    value that could not be read.
    """

    info: DeviceInfo
    api_level: int | None = None
    emulator: bool | None = None


def _getprop(client: AdbClient, serial: str, name: str) -> str | None:
    """Return a device property, or None when it cannot be read."""
    try:
        raw = client.shell(serial, quote_args("getprop", name))
    except UiprobeError:
        return None
    return raw.decode("utf-8", errors="replace").strip()


def device_details(
    *,
    adb_server: str | None = None,
    adb: str | os.PathLike[str] | None = None,
    start_server: bool = True,
) -> list[DeviceDetails]:
    """List the devices and ask each ready one for its API level and kind.

    One client and one device listing serve every query. A property that
    cannot be read is None; it never fails the listing.
    """
    client = _make_client(adb_server, adb, start_server=start_server)
    details: list[DeviceDetails] = []
    for info in client.devices():
        api_level: int | None = None
        emulator: bool | None = None
        if info.state == READY_STATE:
            level = _getprop(client, info.serial, "ro.build.version.sdk")
            api_level = None if level is None else ascii_int(level)
            qemu = _getprop(client, info.serial, "ro.kernel.qemu")
            emulator = None if qemu is None else qemu == "1"
        details.append(DeviceDetails(info, api_level, emulator))
    return details


def _parse_identity(text: str) -> DeviceIdentity | None:
    """Read the answer to `IDENTITY_COMMAND`; None unless it has four lines."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(DeviceIdentity._fields):
        return None
    qemu, serialno, fingerprint, boot_id = (line.rstrip("\r").strip() for line in lines)
    if not _BOOT_ID.match(boot_id):
        boot_id = ""
    return DeviceIdentity(qemu == "1", serialno, fingerprint, boot_id)
