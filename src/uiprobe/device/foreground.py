"""`_ForegroundMixin`: the app in front and the window that has the focus."""

from __future__ import annotations

# Imported at run time, not only for type checking, so that
# typing.get_type_hints() resolves the ``owned`` of Device.window_at and
# Device.diagnose_tap_window.
from collections.abc import Callable  # noqa: TC003

from uiprobe.device.focus import (
    _ABSENT,
    _FOCUSED_APP_SOURCE,
    _INPUT_DISPLAY,
    _RESUMED_SOURCE,
    FocusedWindow,
    _display_zero_focused,
    _dump_lines,
    _names_a_display,
    _names_other_display,
    _read_resumed_of_several,
    _read_source,
    _read_window_owner,
    _window_focus,
)
from uiprobe.device.select import (
    _OPTIONAL_ERRORS,
    IDENTITY_COMMAND,
    DeviceIdentity,
    _parse_identity,
)
from uiprobe.device.state import _DeviceState
from uiprobe.errors import AdbError, DeviceNotFoundError
from uiprobe.inputwindows import InputWindow, read_window_at
from uiprobe.windowdiag import TapWindowDiagnosis, diagnose_tap_window


class _ForegroundMixin(_DeviceState):
    """Reading the app in front and the window that has the focus."""

    def foreground(self) -> str | None:
        """Return the package of the app that has focus, or None when unknown.

        The package comes from the same trusted sources as the activity in
        `dump`; it is whatever package that source names, not filtered. None
        means the output does not tell it reliably (missing, malformed,
        forged or ambiguous). On a device with several displays (a foldable
        with a cover screen) it is read only while ``mTopFocusedDisplayId``
        says display 0 has the focus, and then from the resumed activity
        alone (see `_trusted_component`); otherwise None.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        component = self._trusted_component()
        return None if component is None else component[0]

    def focused_window(self) -> FocusedWindow | None:
        """Return the window that has input focus: its owner and its title.

        ``mCurrentFocus`` in ``dumpsys window`` names the window (``Window{HASH
        u0 TITLE}``); its block in ``dumpsys window windows`` (``Window #N
        Window{HASH u0 TITLE}:``) has, two lines below, the line ``mOwnerUid=...
        package=OWNER``, which the system writes. The owner comes from that
        line, never from the title, which an app chooses. Captured in the same
        form on emulators at API 24, 30 and 36; the notification shade is then
        a window of ``com.android.systemui``.

        None means unknown: no or several ``mCurrentFocus`` values, ``null``,
        a malformed line, no block or several blocks with that hash (titles
        can hold newlines, so a forged block can be added but the genuine one
        cannot be removed), a block whose title differs, or an owner line of
        another shape. The two dumps are read one after the other, so a focus
        that changed in between gives None too. On a device with several
        displays it is read only while ``mTopFocusedDisplayId`` says display
        0 has the focus; ``null`` lines are then left out and the other
        ``mCurrentFocus`` lines must name one window (see
        `_read_focus_of_several`); otherwise None.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        # The reading (one display or several) is decided once, from the
        # first dump; a later read of this call that disagrees gives None.
        text = self._shell("dumpsys window")
        lines = _dump_lines(text)
        several = self._note_dump(lines)
        focus = _window_focus(text, lines, several=several)
        if focus is None:
            return None
        windows = self._shell("dumpsys window windows")
        windows_lines = _dump_lines(windows)
        if (self._note_dump(windows_lines) and not several) or _names_a_display(
            windows_lines
        ):
            return None
        found = _read_window_owner(windows, *focus, display_zero=several)
        # Read the focus again: a window that took it between the two dumps
        # would otherwise be given the old window's owner.
        if found is None:
            return None
        again = self._shell("dumpsys window")
        again_lines = _dump_lines(again)
        if self._note_dump(again_lines) != several:
            return None
        return (
            found
            if _window_focus(again, again_lines, several=several) == focus
            else None
        )

    def identity(self) -> DeviceIdentity | None:
        """Read whether this is an emulator, its serial number and build, at once.

        One command reads ``ro.kernel.qemu``, ``ro.serialno``,
        ``ro.build.fingerprint`` and ``/proc/sys/kernel/random/boot_id``. None
        means the answer did not have exactly those four lines. A boot id that
        is not a lowercase UUID is kept as ``""`` (not readable).

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return _parse_identity(self._shell(IDENTITY_COMMAND))

    def check_pin(self, *, emulator: bool) -> None:
        """Raise unless the pinned transport still leads to the same device.

        The adb server must still list the pinned transport id with this serial
        (a restarted server numbers its transports anew; a renumbered one is
        followed only as `can_repin` allows), and `identity` through it must
        be the pinned device (a pinned boot id read again and the same; a boot
        id that could no longer be read counts as a change; without a pinned
        boot id the three other fields); with `emulator` it must also be an
        emulator. Nothing is sent but these reads. A failure is kept: every
        later request of this `Device` raises it again.

        Raises:
            DeviceNotFoundError: The pinned transport is gone, lists another
                serial, or leads to a device whose identity differs
                (``details["reason"]`` is ``device_changed``).
            AdbError: `pin_transport` was not called, or the reads failed.
        """
        if type(emulator) is not bool:
            msg = f"emulator must be a bool, not {type(emulator).__name__}"
            raise TypeError(msg)
        expected = self._pinned_identity
        if self._transport_id is None or expected is None:
            msg = "the transport is not pinned"
            raise AdbError(msg, details={"serial": self.serial})
        # The read and the comparison are one step: any way out but a match
        # (an interrupt while the answer is parsed, say) makes the proof stale.
        matched = False
        try:
            found = self.identity()  # its request checks the listing first
            matched = self._same_device(found) and not (
                emulator and (found is None or not found.emulator)
            )
        except BaseException as error:
            self._interrupted(error)
            raise
        finally:
            if not matched:
                self._epoch += 1
        if not matched:
            msg = (
                f"the device pinned as {self.serial} (transport"
                f" {self._transport_id}) is not the one that was checked any more"
            )
            details = {"serial": self.serial, "reason": "device_changed"}
            raise self._fail(DeviceNotFoundError(msg, details=details))

        # No mark is set here: the identity read itself went out only with a
        # current proof (see `_check_listed`), which a proof had set.

    def window_at(
        self,
        point: tuple[int, int],
        *,
        owned: Callable[[InputWindow], bool] | None = None,
        named: Callable[[InputWindow], bool] | None = None,
    ) -> InputWindow | str:
        """Return the window a touch at `point` would reach, or why that is
        not known.

        Reads ``dumpsys input`` once and takes the topmost window of display
        0 that takes touches at `point` (see `uiprobe.inputwindows`). The
        string is one of ``no_list``, ``line_format``, ``region_format``,
        ``rotation`` (the display is rotated, or its orientation cannot be
        read), ``no_window`` (no window takes a touch there) or ``portal``
        (the window there forwards touches to another display); the first
        four are an `Unreadable` whose ``rule`` names the check that refused
        (see `diagnose_tap_window`). `owned` tells the app's own windows,
        the only ones that may print ``samsungFlags=0x1`` (see
        `uiprobe.inputwindows.read_window_at`; without it none may). The
        `named` tells the windows whose title names the app: with ``owned``
        it is needed for ``samsungFlags=0x100000`` and ``0x100001`` (without
        it those refuse). The crawl always passes `owned` and `named` by
        keyword. An override in a subclass that does not accept them raises
        `TypeError` on the first guarded tap; one that accepts them but does
        not forward them makes the app's own windows with ``samsungFlags=0x1``
        (and, without `named`, ``0x100000`` and ``0x100001``) refuse the tap
        (``samsung_flags``).
        ``Display: N`` sections of two or more numbers in the read count as
        several displays for `displays_seen`.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        text = self._shell("dumpsys input")
        sections = {
            int(match[1])
            for line in _dump_lines(text)
            if (match := _INPUT_DISPLAY.fullmatch(line)) is not None
        }
        if len(sections) > 1:
            self._several_seen = True
        return read_window_at(text, point, owned=owned, named=named)

    def diagnose_tap_window(
        self,
        *,
        show_device_text: bool = False,
        owned: Callable[[InputWindow], bool] | None = None,
        named: Callable[[InputWindow], bool] | None = None,
    ) -> TapWindowDiagnosis:
        """Tell whether `window_at` can read this device's window list, and if
        not which rule refused it and where, with no text of the device's
        output in the result (see `uiprobe.windowdiag`).

        Reads ``dumpsys input`` once. With `show_device_text` the result
        also holds the shapes of the lines, which hold device text. `owned`
        tells the app's own windows, the only ones that may print
        ``samsungFlags=0x1``, as for `window_at` (``uiprobe diagnose
        tap-window --package`` passes the crawl's test); without it every
        such window of display 0's list refuses (``samsung_flags``).
        `named` adds the title test that ``samsungFlags=0x100000`` and
        ``0x100001`` also need.

        Raises:
            TypeError: `show_device_text` is not a bool, or `owned` or
                `named` is neither None nor callable (before any request).
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if not isinstance(show_device_text, bool):
            msg = "show_device_text must be a bool"
            raise TypeError(msg)
        if owned is not None and not callable(owned):
            msg = "owned must be None or callable"
            raise TypeError(msg)
        if named is not None and not callable(named):
            msg = "named must be None or callable"
            raise TypeError(msg)
        text = self._shell("dumpsys input")
        return diagnose_tap_window(
            text, show_device_text=show_device_text, owned=owned, named=named
        )

    def _trusted_component(self) -> tuple[str, str] | None:
        """Return the focused component from the trusted source, or None.

        ``dumpsys window`` is read first; only when it says nothing is
        ``dumpsys activity activities`` read. A source that speaks but cannot
        be trusted gives None with no fallback. A dump with several displays
        (see `_several_displays`) is read only while every
        ``mTopFocusedDisplayId`` line says display 0, and then its
        ``mFocusedApp`` lines, one per display, are not used: the component
        comes from the activity dump's ``topResumedActivity`` and
        ``ResumedActivity`` alone, which must both be there and agree (see
        `_read_resumed_of_several`). Device errors propagate.
        """
        text = self._shell("dumpsys window")
        lines = _dump_lines(text)
        if self._note_dump(lines):
            self._other_display = _names_other_display(lines)
            if not _display_zero_focused(lines):
                return None
            return _read_resumed_of_several(self._shell("dumpsys activity activities"))
        found = _read_source(text, _FOCUSED_APP_SOURCE)
        if found is _ABSENT:
            found = _read_source(
                self._shell("dumpsys activity activities"), _RESUMED_SOURCE
            )
        return found if isinstance(found, tuple) else None

    def _focused_component(self, package: str) -> tuple[str, str] | None:
        """Return the focused component, only when it belongs to `package`.

        Best effort: any failure, or a result that is missing, ambiguous or of
        another package, gives None.
        """
        if not package:
            return None
        try:
            component = self._trusted_component()
        except _OPTIONAL_ERRORS:
            return None
        if component is None or component[0] != package:
            return None
        return component
