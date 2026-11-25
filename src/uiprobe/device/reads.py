"""`_ReadsMixin`: reading the screen and the device's properties."""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from typing import Final

from uiprobe.adb import ascii_int, quote_args
from uiprobe.crawl_preview import check_package
from uiprobe.device.focus import _read_input_shown, _several_displays
from uiprobe.device.parse import _activity_name, _parse_wm_size
from uiprobe.device.select import _OPTIONAL_ERRORS
from uiprobe.device.state import _DeviceState
from uiprobe.errors import AdbError, DumpError, UiprobeError
from uiprobe.model import Screen, parse_uiautomator_dump
from uiprobe.packageuid import PackageUid, parse_package_uid

DUMP_DIRECTORY: Final = "/data/local/tmp"


DUMP_MESSAGE_LIMIT: Final = 500


# `uiautomator dump` prints nothing until it finishes, and on an animating
# screen it can wait about 10 s for the UI to settle before it gives up. The
# client's 10 s silence limit would fire first, so the dump gets its own.
DUMP_TIMEOUT: Final = 60.0


class _ReadsMixin(_DeviceState):
    """Reading the screen and the device's properties."""

    def api_level(self) -> int | None:
        """Return the Android API level, or None when it cannot be read.

        A failure of the adb server or the device is not an error here and is not
        remembered, so a later call tries again.
        """
        if not self._api_level_known:
            try:
                text = self._shell("getprop ro.build.version.sdk").strip()
            except _OPTIONAL_ERRORS:
                return None
            self._api_level = ascii_int(text)
            self._api_level_known = True
        return self._api_level

    def is_emulator(self) -> bool:
        """Return True only when the device is an emulator.

        ``getprop ro.kernel.qemu`` must print exactly ``1`` (surrounding
        whitespace and a CR are ignored). An empty answer, ``0`` or anything
        else is not an emulator. A failure is never read as an answer.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return self._shell("getprop ro.kernel.qemu").strip() == "1"

    def package_uid(self, package: str) -> PackageUid | None:
        """Return the app id the system gave `package` and whether the package
        shares it, None when not readable.

        Reads ``userId=`` or ``appId=`` and ``sharedUser=`` from the
        package's own stanza of ``dumpsys package <package>`` (see
        `uiprobe.packageuid`); a package that is not installed, or whose
        stanzas disagree, gives None. The app id is the uid of the package's
        windows for Android user 0 only. It can change when the app is
        installed again.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before any request).
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if isinstance(package, str):
            package = str.__str__(package)
        check_package(package)
        text = self._shell(quote_args("dumpsys", "package", package))
        return parse_package_uid(text, package)

    def keyboard_shown(self) -> bool | None:
        """Return whether the soft keyboard is shown, or None when unknown.

        Reads ``mInputShown`` from ``dumpsys input_method``. None means no line
        claims the field, a claiming line is malformed (an app may print one),
        or the claiming lines disagree.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return _read_input_shown(self._shell("dumpsys input_method"))

    def dump(self, *, details: bool = True) -> Screen:
        """Capture the current screen with ``uiautomator dump``.

        The returned `Screen` becomes this device's latest screen: `tap`
        accepts only elements of that screen. A failed dump keeps the previous
        one.

        With `details` True (the default) the activity (``dumpsys window``) and
        the API level (``getprop``) are read too. With `details` False those two
        reads are skipped and `Screen.activity` and `Screen.api_level` are None;
        the display size (``wm size``) and everything the visibility and cover
        checks need are still read. `Screen.screen_id` includes the activity, so
        it can differ from the id of a full dump of the same screen.

        Raises:
            TypeError: `details` is not a bool (nothing is sent).
            DumpError: uiautomator failed or timed out, or its XML is unusable.
        """
        if not isinstance(details, bool):
            msg = f"details must be a bool, not {type(details).__name__}"
            raise TypeError(msg)
        xml = self._dump_xml()
        parsed = parse_uiautomator_dump(xml)
        elements = parsed.elements
        tops = [e for e in elements if e.parent is None]
        package = tops[0].package if tops else ""
        measured = self._display_size(parsed.rotation)
        display = measured or (
            max((e.bounds.right for e in tops), default=0),
            max((e.bounds.bottom for e in tops), default=0),
        )
        activity = None
        if details:
            component = self._focused_component(package)
            if component is not None:
                activity = _activity_name(package, component)
        screen = Screen(
            serial=self.serial,
            captured_at=datetime.now(UTC).astimezone().replace(microsecond=0),
            package=package,
            activity=activity,
            api_level=self.api_level() if details else None,
            display=display,
            elements=tuple(elements),
            rotation=parsed.rotation,
        )
        self._latest_screen = screen
        self._latest_display_measured = measured is not None
        return screen

    def _dump_xml(self) -> bytes:
        path = f"{DUMP_DIRECTORY}/uiprobe-{secrets.token_hex(8)}.xml"
        try:
            output = self._run_uiautomator(path)
            if "dumped to" not in output:
                raise DumpError(
                    "uiautomator dump failed",
                    details={"output": output.strip()[:DUMP_MESSAGE_LIMIT]},
                )
            return self._call(
                lambda pin: self._client.exec_out(
                    self.serial, quote_args("cat", path), **pin
                ),
                strict=False,
            )
        finally:
            with contextlib.suppress(UiprobeError):
                self._shell(quote_args("rm", "-f", path))

    def _run_uiautomator(self, path: str) -> str:
        """Run ``uiautomator dump``.

        Only a timeout while reading its output, after the service answered
        OKAY, is a `DumpError`; a stall in the handshake stays an `AdbError`.
        """
        try:
            return self._shell_text(
                quote_args("uiautomator", "dump", path), timeout=DUMP_TIMEOUT
            )
        except AdbError as error:
            if (
                error.details.get("reason") != "timeout"
                or error.details.get("stage") != "output"
            ):
                raise
            raise DumpError(
                "uiautomator dump timed out",
                details={
                    "reason": "timeout",
                    "timeout_seconds": error.details.get("timeout_seconds"),
                },
            ) from error

    def _display_size(self, rotation: int) -> tuple[int, int] | None:
        """Return the current logical display size, or None when unreadable."""
        try:
            size = _parse_wm_size(self._shell("wm size"))
        except _OPTIONAL_ERRORS:
            return None
        if size is None:
            return None
        # `wm size` reports the natural orientation.
        return (size[1], size[0]) if rotation in {1, 3} else size

    def _note_dump(self, lines: list[str]) -> bool:
        """Whether a ``dumpsys window`` read shows several displays; once one
        has, `displays_seen` stays True until `forget_displays` (a crawl run
        calls it when it starts)."""
        several = _several_displays(lines)
        if several:
            self._several_seen = True
        return several
