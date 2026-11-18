"""Error types shared by the library and the command line.

Every problem with a device, adb, or the data it returns is a `UiprobeError`
whose `code` is the same string the command line prints with ``--json``.
Mistakes in the caller's arguments stay `TypeError` and `ValueError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "INTERNAL_ERROR",
    "INTERRUPTED",
    "USAGE_ERROR",
    "AdbError",
    "AdbServerUnavailableError",
    "DeviceNotFoundError",
    "DeviceOfflineError",
    "DeviceUnauthorizedError",
    "DumpError",
    "ElementNotFoundError",
    "ElementNotVisibleError",
    "InputFailedError",
    "MultipleDevicesError",
    "MultipleElementsError",
    "UiprobeError",
]

ERROR_SCHEMA = "uiprobe.error/1"

# (code, exit code) pairs for failures that only the command line produces.
USAGE_ERROR: tuple[str, int] = ("usage_error", 2)
INTERRUPTED: tuple[str, int] = ("interrupted", 130)
INTERNAL_ERROR: tuple[str, int] = ("internal_error", 1)


class UiprobeError(Exception):
    """Base class of every error uiprobe raises on purpose.

    Attributes:
        message: Human-readable description.
        details: Machine-readable context; empty when there is none.
    """

    code: ClassVar[str] = "internal_error"
    exit_code: ClassVar[int] = 1

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = dict(details) if details else {}

    def to_dict(self) -> dict[str, object]:
        """Return the ``uiprobe.error/1`` JSON object for this error."""
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"schema": ERROR_SCHEMA, "error": error}


class AdbServerUnavailableError(UiprobeError):
    """The adb server cannot be reached."""

    code = "adb_server_unavailable"
    exit_code = 3


class DeviceNotFoundError(UiprobeError):
    """No device matches the request."""

    code = "device_not_found"
    exit_code = 4


class MultipleDevicesError(UiprobeError):
    """More than one usable device is connected and none was chosen."""

    code = "multiple_devices"
    exit_code = 4


class DeviceOfflineError(UiprobeError):
    """The device is connected but not usable (offline or similar)."""

    code = "device_offline"
    exit_code = 5


class DeviceUnauthorizedError(UiprobeError):
    """The device has not authorized this computer for debugging."""

    code = "device_unauthorized"
    exit_code = 5


class AdbError(UiprobeError):
    """The adb server answered FAIL, or the protocol broke."""

    code = "adb_error"
    exit_code = 10


class DumpError(UiprobeError):
    """The uiautomator dump failed or its XML is unusable."""

    code = "dump_failed"
    exit_code = 11


class ElementNotFoundError(UiprobeError):
    """No element matches the criteria."""

    code = "element_not_found"
    exit_code = 12


class MultipleElementsError(UiprobeError):
    """More than one element matches the criteria."""

    code = "multiple_elements"
    exit_code = 13


class ElementNotVisibleError(UiprobeError):
    """The element cannot be pressed: nothing, too little, or only a covered part shows.

    ``details["reason"]`` is ``no_visible_part``, ``too_small`` (the part on the
    display is under 8 pixels wide or high) or ``covered`` (another meaningful
    element is on top of the press point, or, for an element that is not
    meaningful, an ancestor would receive it; ``covered_by`` is its id and
    ``point`` the press point) or ``no_target`` (no meaningful element holds
    the press point, so nothing there can receive a press; ``point`` is it).
    ``too_small`` adds ``visible_size``, the
    ``[width, height]`` of the part on the display.
    """

    code = "element_not_visible"
    exit_code = 12


class InputFailedError(UiprobeError):
    """The device did not report success for an input command.

    Success needs the marker line ``uiprobe-exit:0`` as the last line and no
    other line that is not empty; empty lines are ignored.

    This does not prove that nothing was pressed: ``input`` may have acted
    before it failed, so the input must not be repeated blindly.
    """

    code = "input_failed"
    exit_code = 14
