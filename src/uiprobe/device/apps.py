"""`_AppsMixin`: starting and stopping apps, and screenshots."""

from __future__ import annotations

from typing import Final

from uiprobe.adb import quote_args
from uiprobe.also_target import HOME_COMMAND
from uiprobe.crawl_preview import check_package
from uiprobe.device.displays import parse_active_display_physical_id
from uiprobe.device.failure import _EXIT_SUFFIX, _escaped, _escaped_line, _input_failure
from uiprobe.device.launch import (
    _FOREGROUND_LIMIT,
    _check_launch_timeout,
    _home_package,
    _launcher_component,
    _shown_foreground,
    _start_failure,
)
from uiprobe.device.select import PIN_NOT_PROVEN, _Dispatch, _dispatch
from uiprobe.device.state import _DeviceState
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
    LaunchFailedError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.pngfile import check_png

DEFAULT_LAUNCH_TIMEOUT: Final = 10.0


# What `screencap` prints, as text before the image, on a device with more than
# one display (only the stable start is matched).
MULTIPLE_DISPLAYS_WARNING: Final = b"[Warning] Multiple displays were found"


LAUNCH_POLL_INTERVAL: Final = 0.5


# An app is started with ``am start -W`` of its launcher activity, read first
# with ``cmd package resolve-activity --brief``. ``monkey -p PACKAGE -c
# android.intent.category.LAUNCHER 1`` is not used: it switched the system
# setting accelerometer_rotation (auto-rotate) from 0 to 1 on a Samsung API 36
# phone and on API 24 and 36 emulators, while ``am start -W`` left it as it was.
LAUNCHER_CATEGORY: Final = "android.intent.category.LAUNCHER"


MAIN_ACTION: Final = "android.intent.action.MAIN"


# The silence ``am start -W`` may keep while it waits is the launch timeout,
# but at least this long (a read timeout of 0 would not wait at all).
START_MIN_WAIT: Final = 1.0


# `screencap` prints nothing until the image is encoded, which a slow device
# can take longer than the client's 10 s silence limit.
SCREENSHOT_TIMEOUT: Final = 30.0


def _png_error(data: bytes) -> ValueError | None:
    try:
        check_png(data)
    except ValueError as error:
        return error
    return None


class _AppsMixin(_DeviceState):
    """Starting and stopping apps, and screenshots."""

    def launch(
        self,
        package: str,
        *,
        restart: bool = False,
        timeout: float = DEFAULT_LAUNCH_TIMEOUT,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """Start the app `package` and wait until it is in front.

        Earlier versions started apps with ``monkey -p PACKAGE -c
        android.intent.category.LAUNCHER 1``, which turned auto-rotate on
        (``accelerometer_rotation`` 0 to 1; seen on API 24 and 36 emulators
        and a Samsung API 36 phone). ``am start`` does not change it, and
        ``monkey`` is never sent.

        First the launcher activity is read with ``cmd package
        resolve-activity --brief -c android.intent.category.LAUNCHER PACKAGE;
        echo uiprobe-exit:$?`` (see `launcher_activity`), before every start.
        The read goes out only to a device that needs no
        new proof, as a request that changes it would, and when anything
        happened to the pin during it (`pin_epoch` moved) nothing more is sent
        (`DeviceNotFoundError` with ``pin_not_proven``). When the activity
        cannot be read, nothing is sent that changes the device.
        With ``restart=True`` the command ``am force-stop PACKAGE; echo
        uiprobe-exit:$?`` is sent next. It stops the app, so the next launch
        starts a fresh task; state the app has not saved is lost. It must
        report exit status 0 and print nothing else (empty lines are ignored),
        or nothing more is sent. The launcher activity is then read again in
        the same way, and the start uses that second read (the launcher can
        change while the app stops); when it cannot be read, the start is not
        sent and the error says ``force_stopped`` (stopped, not started).
        Then exactly ``am start -W -a android.intent.action.MAIN -c
        android.intent.category.LAUNCHER -n PACKAGE/CLASS; echo
        uiprobe-exit:$?`` is sent. It succeeds only when no line holds a
        control character (after one CR at its end is cut), no line starts
        with ``Error``, the exit status is 0, and the other lines are, each
        at most once: exactly one ``Starting: Intent {
        act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER]
        cmp=PACKAGE/CLASS }`` naming the component sent (short or full
        spelling), one of the two ``Warning:`` lines seen when the app was
        running, exactly one ``Status: ok``, exactly one ``Activity:`` line
        naming an activity of `package` (its class may differ from the one
        started), ``LaunchState:``, ``ThisTime:``, ``TotalTime:``,
        ``WaitTime:`` and ``Complete``. The wait of ``-W`` may stay silent for
        `timeout` seconds (at least `START_MIN_WAIT`). Each command is sent
        once and never resent, not even after a connection error.

        Then `foreground` is read about every 0.5 seconds until it equals
        `package`. It is read at least once, and no read starts after
        `timeout` seconds have passed: the timeout bounds when the last read
        may start, not the whole wait. A read that started in time still
        counts, so the wait can run past the timeout by the time of that read,
        and by how late a pause ends when the system wakes it late.

        A start whose reply names an activity of another app (or of the
        system) is waited on in the same way: when the app comes to the front
        within `timeout`, the launch succeeds; otherwise it fails with
        ``other_activity``. A str subclass is used as a plain str with the
        same text.

        No request of a launch starts an adb server: it uses only the server
        that is already there (`AdbServerUnavailableError`, ``start_skipped``
        ``in_launch``, when it is gone). After its first request, a request
        that fails (also one a read swallows) ends the launch (`AdbError` with
        ``launch_interrupted`` when the error itself was swallowed).
        On a device that is not pinned (as ``uiprobe launch`` uses), each
        request selects the device by its serial. Accepted limit: when the
        device behind the serial is replaced between the launcher read and
        the start, the start uses the component read just before (it fails,
        or opens that component of the same package on the new device). A
        pinned device keeps its pin's rule.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        `_send_by` is a value of uiprobe's own clock by which the stop and the
        start must go out, and `_epoch` the `pin_epoch` the crawl approved
        (both checked on the selected connection right before each request
        is written; ``send_stopped``, ``pin_not_proven``). The foreground
        reads after the start are bounded by `timeout` only.

        Raises:
            TypeError: `package` is not a str, `restart` is not a bool, or
                `timeout` is not a number (a bool is not).
            ValueError: `package` does not follow the Android package rule
                (see `uiprobe.crawl_preview.PACKAGE_PATTERN`), or `timeout` is
                not from 0 to 600 seconds. Both are raised before the adb
                server is contacted.
            LaunchFailedError: ``details`` hold ``package`` and ``reason``:
                ``launcher_unreadable`` (the launcher activity could not be
                read; ``output`` is the first line that is not blank, stripped,
                cut to 200 characters and escaped; nothing was sent, or, with
                ``force_stopped`` True, the read after the force-stop failed:
                the app was stopped and not started),
                ``stop_failed`` (``am force-stop`` did not report success;
                ``exit_status`` when it reported a status other than 0,
                ``output`` when it printed something else, escaped; with no
                status and no output neither is set; the start was not sent),
                ``start_failed`` (``am start`` printed a line starting with
                ``Error``, or ended with an exit status other than 0, then in
                ``exit_status``), ``other_activity`` (a reply that is a
                success in every other way named an activity of another app
                or of the system (``android``, its chooser), and the app was
                still not in front after the wait; ``activity`` is that
                component, escaped),
                ``unexpected_output`` (``am start`` did not report the success
                described above), these three with ``output``, the
                line at fault or else the first line that is not blank, cut to
                200 characters and escaped, ``start_timeout`` (the reply of
                ``am start -W`` did not come: it was silent for ``timeout``
                seconds, the read timeout used) or ``not_in_front`` (the app
                was not the foreground app in time; ``foreground`` is the last
                package seen (escaped; past 1000 characters cut and ended with
                ``...``), or None, and ``timeout`` the limit). After
                ``start_failed``, ``other_activity``, ``unexpected_output``,
                ``start_timeout`` and ``not_in_front`` the app may still have
                started.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone, or has to be proven
                again (``pin_not_proven``, nothing sent).
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if isinstance(package, str):
            package = str.__str__(package)  # a plain str, whatever the subclass
        package = check_package(package)
        if not isinstance(restart, bool):
            msg = f"restart must be a bool, not {type(restart).__name__}"
            raise TypeError(msg)
        limit = _check_launch_timeout(timeout)
        dispatch = _dispatch(_send_by, _epoch)
        self._launch(package, limit, restart=restart, dispatch=dispatch)

    def _launch(
        self,
        package: str,
        limit: float,
        *,
        restart: bool,
        dispatch: _Dispatch | None,
        activity: str | None = None,
    ) -> None:
        """`launch` after its arguments were checked.

        No request of the launch starts an adb server: it uses only a server
        that is already there, so a server that went away cannot be replaced
        by one that lists another device under the serial. After the first
        request, a request that fails (also one a read swallows) ends it.
        `activity`, when given, is a launcher activity the caller read with
        `_start_component` right before (the crawl: then its last checks,
        then the start, with nothing read in between).
        """
        failed = self._failed_requests
        self._launch_holds += 1
        try:
            self._launch_steps(
                package, limit, restart=restart, dispatch=dispatch, activity=activity
            )
        finally:
            self._launch_holds -= 1
        self._check_no_failure(failed)

    def _launch_component(
        self,
        package: str,
        activity: str,
        *,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """`launch` of `package` (checked by the caller) that starts
        `activity`, read by the caller with `_start_component` right before,
        without reading it again (the crawl's first launch and relaunch)."""
        dispatch = _dispatch(_send_by, _epoch)
        self._launch(
            package,
            DEFAULT_LAUNCH_TIMEOUT,
            restart=False,
            dispatch=dispatch,
            activity=activity,
        )

    def _start_component(self, package: str) -> str:
        """The launcher read before a start: `launcher_activity` sent only to
        a device proven now, as a start would be, and refused
        (``pin_not_proven``) when anything happened to the pin during it."""
        epoch = self._epoch
        activity = self._launcher_activity(package, strict=True)
        if self._epoch != epoch:
            msg = (
                f"{self.serial} has to be proven again before an input"
                f" (transport {self._transport_id}); nothing was sent"
            )
            details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
            raise DeviceNotFoundError(msg, details=details)
        return activity

    def _start_component_after_stop(self, package: str) -> str:
        """`_start_component` after the app was force-stopped: every failure
        says so (``force_stopped``: stopped, not started)."""
        try:
            return self._start_component(package)
        except LaunchFailedError as error:
            msg = f"{package} was force-stopped but not started again: {error}"
            details = {**error.details, "force_stopped": True}
            raise LaunchFailedError(msg, details=details) from None
        except DeviceNotFoundError as error:
            if error.details.get("reason") != PIN_NOT_PROVEN:
                raise
            msg = (
                f"{package} was force-stopped, but the start was not sent: the"
                f" device has to be proven again"
            )
            details = {**error.details, "force_stopped": True}
            raise DeviceNotFoundError(msg, details=details) from None
        except UiprobeError as error:
            # Any other failure of the read (a transport error, a timeout)
            # also says that the app was stopped and not started.
            error.details = {**error.details, "force_stopped": True}
            raise

    def _check_no_failure(self, failed: int) -> None:
        """Raise when a request failed since `failed` was taken in a launch."""
        if self._failed_requests != failed:
            msg = (
                f"a request to {self.serial} failed during the launch; nothing"
                " that changes the device was sent after it"
            )
            raise AdbError(msg, details={"reason": "launch_interrupted"})

    def _launch_steps(
        self,
        package: str,
        limit: float,
        *,
        restart: bool,
        dispatch: _Dispatch | None,
        activity: str | None = None,
    ) -> None:
        """The launcher reads, the stop, the start and the foreground reads."""
        failed = self._failed_requests
        if activity is None:
            # The launcher is read before every start. With a restart it is
            # read before the stop (an app whose launcher cannot be read is
            # not stopped) and again after it, and the start uses the second
            # read: the launcher can change while the app stops.
            activity = self._start_component(package)
            failed = self._failed_requests
            if restart:
                self._force_stop(package, dispatch)
                self._check_no_failure(failed)
                activity = self._start_component_after_stop(package)
        elif restart:
            self._force_stop(package, dispatch)
        self._check_no_failure(failed)
        # A start that named an activity of another app is waited on like
        # any other: the app may come to the front in time.
        other = self._start(
            package, activity, limit, force_stopped=restart, dispatch=dispatch
        )
        details: dict[str, object] = {"package": package}
        deadline = self._clock() + limit
        while True:
            self._check_no_failure(failed)
            self._other_display = False
            seen = self.foreground()
            # A read can succeed after a failure inside it (a wait that read
            # the identity again), also on the last poll: that ends it too.
            self._check_no_failure(failed)
            if seen == package:
                return
            if self._clock() >= deadline:
                break
            self._sleep(max(0.0, min(LAUNCH_POLL_INTERVAL, deadline - self._clock())))
            if self._clock() > deadline:
                break  # no read starts after the deadline
        if other is not None:
            raise other
        shown = None if seen is None else _shown_foreground(seen)
        details.update(reason="not_in_front", foreground=shown, timeout=limit)
        last = "unknown" if shown is None else shown
        if seen is None and self._other_display:
            last += "; another display, perhaps the cover screen, has the focus"
        msg = f"{package} was not in front after {limit:g} seconds (foreground: {last})"
        raise LaunchFailedError(msg, details=details)

    def launcher_activity(self, package: str) -> str:
        """Return the launcher activity of `package` as ``PACKAGE/CLASS``.

        It sends ``cmd package resolve-activity --brief -c
        android.intent.category.LAUNCHER PACKAGE; echo uiprobe-exit:$?`` (a
        read: nothing on the device changes). The output must be zero or more
        lines like ``priority=0 preferredOrder=0 match=0x108000
        specificIndex=-1 isDefault=true``, then exactly one line
        ``PACKAGE/CLASS`` with exactly `package` before the slash and a class
        of dot-separated parts of ASCII letters, digits, ``_`` and ``$`` (it
        may start with ``.``; at most 255 characters), then the line
        ``uiprobe-exit:0`` last, so output cut short and another exit status
        are refused. Empty lines and one CR at each line end are ignored. The
        CLASS is returned as printed, often in the short form ``.Name``.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before the adb server is contacted).
            LaunchFailedError: Anything else was printed (``No activity
                found`` for a package that is not installed or has no
                launcher activity, another package, more lines);
                ``details["reason"]`` is ``launcher_unreadable`` and
                ``details["output"]`` the first line that is not blank,
                stripped, cut to 200 characters and escaped.
            AdbError: The adb protocol or the command failed.
        """
        if isinstance(package, str):
            package = str.__str__(package)
        return self._launcher_activity(check_package(package))

    def home_package(self) -> str | None:
        """Return the package of the device's home app, None when not readable.

        It sends `uiprobe.also_target.HOME_COMMAND` followed by ``; echo
        uiprobe-exit:$?`` (a read: nothing on the device changes) and takes
        the package of the last line before the marker (``pkg/.Activity``).
        None means the output was not of that form (another exit status, no
        component, ``android`` when the system asks which home app to use).

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return _home_package(self._shell_text(HOME_COMMAND + _EXIT_SUFFIX))

    def _launcher_activity(self, package: str, *, strict: bool = False) -> str:
        """Read the launcher activity; `strict` sends the read only to a device
        that needs no new proof (as a request that changes it)."""
        output = self._shell_text(
            quote_args(
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                LAUNCHER_CATEGORY,
                package,
            )
            + _EXIT_SUFFIX,
            strict=strict,
        )
        component = _launcher_component(output, package)
        if component is not None:
            return component
        first = _escaped_line(output)
        details: dict[str, object] = {
            "package": package,
            "reason": "launcher_unreadable",
            "output": first,
        }
        msg = (
            f"the launcher activity of {package} could not be read on this device,"
            f" so it was not started; the output began '{first}'"
        )
        raise LaunchFailedError(msg, details=details)

    def _start(
        self,
        package: str,
        activity: str,
        limit: float,
        *,
        force_stopped: bool,
        dispatch: _Dispatch | None = None,
    ) -> LaunchFailedError | None:
        """Send ``am start -W`` of `activity` once and require it to succeed.

        Return the ``other_activity`` error, not raised, when a reply that is
        a success in every other way named an activity of another app: the
        caller waits for the app to come to the front first.
        """
        command = (
            quote_args(
                "am",
                "start",
                "-W",
                "-a",
                MAIN_ACTION,
                "-c",
                LAUNCHER_CATEGORY,
                "-n",
                activity,
            )
            + _EXIT_SUFFIX
        )
        wait = max(limit, START_MIN_WAIT)
        details: dict[str, object] = {"package": package}
        try:
            output = self._shell_text(command, timeout=wait, dispatch=dispatch)
        except DeviceNotFoundError as error:
            if not force_stopped or error.details.get("reason") != PIN_NOT_PROVEN:
                raise
            msg = (
                f"{package} was force-stopped, but the start was not sent: the"
                f" device has to be proven again"
            )
            details = {**error.details, "force_stopped": True}
            raise DeviceNotFoundError(msg, details=details) from None
        except AdbError as error:
            if (
                error.details.get("reason") != "timeout"
                or error.details.get("stage") != "output"
            ):
                raise
            # The request failed, so the pin was already doubted.
            details.update(reason="start_timeout", timeout=wait)
            msg = (
                f"the reply of am start -W for {package} did not come within"
                f" {wait:g} seconds; whether the app started is unknown"
            )
            raise LaunchFailedError(msg, details=details) from None
        failure = _start_failure(output, package, activity)
        if failure is None:
            return None
        line = _escaped_line(output) if failure.line is None else _escaped(failure.line)
        details.update(reason=failure.reason, output=line)
        if failure.exit_status is not None:
            details["exit_status"] = failure.exit_status
        msg = f"am start of {package} {failure.problem}; its output: '{line}'"
        if failure.other is not None:
            # A complete, well-formed reply: nothing about the pin is in doubt.
            details["activity"] = _escaped(failure.other, _FOREGROUND_LIMIT)
            return LaunchFailedError(msg, details=details)
        raise self._doubt(LaunchFailedError(msg, details=details))

    def stop_app(
        self,
        package: str,
        *,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """Send ``am force-stop PACKAGE`` once and require it to report success.

        It is the first half of ``launch(package, restart=True)``, for a caller
        that wants to decide between the stop and the start. State the app has
        not saved is lost.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before the adb server is contacted).
            LaunchFailedError: The stop did not report exit status 0 with no
                other output (``details["reason"]`` is ``stop_failed``).
            AdbError: The adb protocol or the command failed.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
        if isinstance(package, str):
            package = str.__str__(package)
        self._force_stop(check_package(package), dispatch)

    def _force_stop(self, package: str, dispatch: _Dispatch | None = None) -> None:
        stop = self._shell_text(
            quote_args("am", "force-stop", package) + _EXIT_SUFFIX, dispatch=dispatch
        )
        failure = _input_failure("force-stop", {}, stop)
        if failure is None:
            return
        self._doubt(failure)
        details: dict[str, object] = {"package": package, "reason": "stop_failed"}
        if "exit_status" in failure.details:
            details["exit_status"] = failure.details["exit_status"]
        output = failure.details.get("output")
        if isinstance(output, str):
            details["output"] = _escaped(output)
        msg = (
            f"am force-stop {package} did not report success, so the app was not"
            " launched"
        )
        raise LaunchFailedError(msg, details=details)

    def screenshot(self) -> bytes:
        """Capture the screen as PNG bytes with ``screencap -p``.

        The bytes are checked in memory before they are returned, without
        decoding them (see `uiprobe.pngfile.check_png`): the signature, a
        well-formed header chunk, chunks that fit, image data and the closing
        chunk at the end. Nothing is
        written to the device or to this computer, and `dump` state is not
        touched.

        A screen that forbids screenshots (``FLAG_SECURE``, for example a
        private browser tab) gives empty output on older Android, and on newer
        Android a mostly black image; the latter is a valid PNG and is
        returned as it is.

        A device with more than one display (a foldable) makes ``screencap``
        print a text warning before the image. Then the physical id of the
        display that is on, which is the one the screen read (`dump`) uses, is
        read from ``dumpsys display`` (exactly one display must be on, and both
        places that name it must agree) and the screen is captured again with
        ``screencap -p -d ID``. Any output that is not a PNG
        and begins with the warning starts this, also one that is only the
        warning text. A device with one display sends only the first command.

        Raises:
            ScreenshotFailedError: The output is empty or is not a complete
                PNG image, or the display could not be told on a device with
                several (``details["reason"]`` is ``multiple_displays_unresolved``).
        """
        data = self._screencap()
        error = _png_error(data)
        if error is None:
            return data
        if not data.startswith(MULTIPLE_DISPLAYS_WARNING):
            raise self._screenshot_failure(data, error) from error
        # A device with several displays warns in text and then shows the
        # first display it finds. Take the one that is on, which `dump` reads, by
        # its physical id instead.
        physical = parse_active_display_physical_id(self._shell("dumpsys display"))
        if physical is None:
            msg = (
                "this device has more than one display and uiprobe could not tell "
                "which display is on (the one the screen read, dump, uses), so no "
                "screenshot was taken"
            )
            raise ScreenshotFailedError(
                msg,
                details={
                    "reason": "multiple_displays_unresolved",
                    "bytes": len(data),
                },
            )
        shot = self._screencap(physical)
        second = _png_error(shot)
        if second is not None:
            raise self._screenshot_failure(shot, second) from second
        return shot

    def _screencap(self, display: str | None = None) -> bytes:
        command = ["screencap", "-p"]
        if display is not None:
            command += ["-d", display]
        return self._call(
            lambda pin: self._client.exec_out(
                self.serial,
                quote_args(*command),
                timeout=SCREENSHOT_TIMEOUT,
                **pin,
            ),
            strict=False,
        )

    @staticmethod
    def _screenshot_failure(data: bytes, error: ValueError) -> ScreenshotFailedError:
        reason = "empty" if not data else "invalid"
        return ScreenshotFailedError(
            f"{error}; the screen may forbid screenshots (for example a "
            "private browser tab), and its structure can still be dumped "
            "with uiprobe dump",
            details={"reason": reason, "bytes": len(data)},
        )

    def display_for_image(self, width: int, height: int) -> tuple[int, int] | None:
        """Return the display size that `dump` reports, turned like an image.

        The size comes from ``wm size``, as in `dump`. A screenshot has no
        rotation of its own, so the size is turned to the orientation of an
        image that is `width` x `height` pixels (a square image keeps the
        natural orientation). None means the size could not be read.
        """
        size = self._display_size(0)
        if size is None:
            return None
        if width != height and (width > height) != (size[0] > size[1]):
            return (size[1], size[0])
        return size
