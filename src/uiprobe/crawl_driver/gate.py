"""The gate: the checks before every input and start goes out."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from uiprobe.crawl import Back, Launch, Observation, Tap
from uiprobe.crawl_driver.constants import (
    _SEND_PATHS,
    FOCUS_CAUSE_SEVERAL_DISPLAYS,
    NOT_SENT_CHANGED,
    NOT_SENT_COVER_UNKNOWN,
    NOT_SENT_COVERED,
    NOT_SENT_FOCUS_CHANGED,
    NOT_SENT_FOCUS_OTHER,
    NOT_SENT_FOCUS_UNKNOWN,
    NOT_SENT_FOREGROUND_UNKNOWN,
    NOT_SENT_KEYBOARD_SHOWN,
    NOT_SENT_KEYBOARD_UNKNOWN,
    NOT_SENT_RECONNECTED,
    NOT_SENT_REFUSED,
    NOT_SENT_STOPPED,
    NOT_SENT_TIME_LIMIT,
)
from uiprobe.crawl_driver.helpers import (
    _app_window,
    _failure_word,
    _Seen,
    _unreadable_launcher,
)
from uiprobe.crawl_driver.state import _RunState
from uiprobe.device import PIN_NOT_PROVEN, PIN_WAIT_STOPPED, displays_seen
from uiprobe.errors import DeviceNotFoundError, LaunchFailedError, UiprobeError
from uiprobe.inputwindows import ROTATION, Unreadable
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from uiprobe.crawl import Action
    from uiprobe.device import FocusedWindow
    from uiprobe.inputwindows import InputWindow
    from uiprobe.model import Screen
    from uiprobe.packageuid import PackageUid


class _GateMixin(_RunState):
    """The gate of a run: the checks before an input or a start goes out."""

    def final_check(self, path: str, approved: int) -> bool:
        """The last comparison before a request that changes the device.

        True when nothing happened to the pin since `approved` (the epoch the
        reads before it began at). The value is kept and handed to the device
        with the request, which compares it once more on the request's own
        connection. No code of the caller runs from here to the request.
        """
        if path not in _SEND_PATHS:
            msg = f"not a sending path: {path!r}"
            raise ValueError(msg)
        if not self.epoch_is(approved):
            return False
        self.approved = approved
        return True

    def epoch_is(self, approved: int) -> bool:
        """The comparison of `final_check` itself: the pin epoch is still
        `approved`."""
        return self.device.pin_epoch == approved

    def cover(
        self,
        point: tuple[int, int],
        focus: tuple[FocusedWindow | None, ...],
        screen: Screen | None,
    ) -> tuple[str, dict[str, str | int]] | None:
        """Why a tap at `point` must not go out, from the window there, or
        None when the window that takes it belongs to the app.

        `focus` is the focused window as read before and after the dump: a
        user other than 0 refuses (``target_user``), and so does an owner uid
        (``mOwnerUid=``) other than the app's (``target_uid``).
        `screen` is the gate's own dump: a hierarchy rotated by the dump's
        ``rotation`` attribute refuses (``rotation``), even when the input
        reader's Viewport lines say 0; a dump that could not be read leaves
        this to the other checks (the engine's re-check refuses it).
        The app's uid is read again right before the window (the app may
        have been installed again under another uid): it must be readable
        and the same as at the start (``target_uid``).

        The window's owner uid must be the app's app id exactly, so only
        Android user 0 passes (another user's uid is user * 100000 + app id).
        When the app shares its uid with other packages (``sharedUser``) or
        has a uid below 10000 (a system uid such as Settings' 1000, which
        system windows have), the window's title must also name the app: be
        the package or begin with the package and ``/`` (as an activity's
        window does); a window of the app with another title is refused.
        The same test tells the reader which windows may print
        ``samsungFlags=0x1`` (`uiprobe.inputwindows.read_window_at`).
        """
        package = self.tap_package(screen)
        app = self.app_uid if package == self.package else self.also_uids.get(package)
        unknown = (
            "target_uid" if app is None else self.uncovered(app, focus, screen, package)
        )
        if app is None or unknown is not None:
            return NOT_SENT_COVER_UNKNOWN, {"unreadable": unknown or "target_uid"}

        def owned(window: InputWindow) -> bool:
            return self.app_window(window, app, package)

        found = self.device.window_at(point, owned=owned)
        if isinstance(found, Unreadable):  # the reason, and the check that refused
            return NOT_SENT_COVER_UNKNOWN, {
                "unreadable": str(found),
                "rule": found.rule,
            }
        if isinstance(found, str):
            return NOT_SENT_COVER_UNKNOWN, {"unreadable": found}
        if self.app_window(found, app, package):
            return None
        return NOT_SENT_COVERED, {"window": found.title, "owner_uid": found.owner_uid}

    def tap_package(self, screen: Screen | None) -> str:
        """The package whose window must take a tap on `screen`: the screen's
        own package when it is one of `Policy.also_target`, else the app (a
        screen of any other package is refused by the engine's re-check)."""
        if screen is not None and screen.package in self.also:
            return screen.package
        return self.package

    def app_window(
        self, window: InputWindow, app: PackageUid, package: str | None = None
    ) -> bool:
        return _app_window(self.package if package is None else package, window, app)

    def uncovered(
        self,
        app: PackageUid,
        focus: tuple[FocusedWindow | None, ...],
        screen: Screen | None,
        package: str | None = None,
    ) -> str | None:
        """What makes the window at a tap's point not worth reading (see
        `cover`), checked in this order, or None.

        `focus` are the focused window as read before and after the dump:
        each one's user must be 0 (``target_user``) and its owner uid, when
        read, the app's uid (``target_uid``).
        """
        users = {f.user for f in focus if f is not None}
        owners = [f.owner_uid for f in focus if f is not None]
        if users - {0}:
            return "target_user"
        if any(uid is not None and uid != app.uid for uid in owners):
            return "target_uid"
        if screen is not None and screen.rotation != 0:
            return ROTATION
        if self.device.package_uid(self.package if package is None else package) != app:
            return "target_uid"
        return None

    def read_state(
        self, action: Action
    ) -> tuple[str | None, FocusedWindow | None, bool | None]:
        """Read the foreground app, the focused window and the keyboard.

        A tap reads them in this order (the keyboard last); a BACK goes to
        the focused window, so for it the focus is the last read.
        """
        foreground = self.device.foreground()
        if isinstance(action, Back):
            keyboard = self.device.keyboard_shown()
            return foreground, self.device.focused_window(), keyboard
        focus = self.device.focused_window()
        return foreground, focus, self.device.keyboard_shown()

    def back_uid(
        self, action: Action, observation: Observation | None
    ) -> PackageUid | None:
        """The app's uid, read now, for a BACK decided on a screen of the app
        (None for any other action, with the tap window check off, or when it
        cannot be read).

        It is asked before the gate's reads of the foreground, focus and
        keyboard, so the focused window is read after it and nothing slow runs
        between that read and the decision. A read that fails is a uid that is
        not readable: the BACK is then not sent, as with a uid that differs.
        """
        if (
            not isinstance(action, Back)
            or observation is None
            or observation.screen is None
            or not self.policy.check_tap_window
            or not self.on_target(observation.screen, observation.foreground)
        ):
            return None
        try:
            return self.device.package_uid(self.tap_package(observation.screen))
        except UiprobeError:
            return None

    def back_window(
        self, focus: FocusedWindow | None, package: str, fresh: PackageUid | None
    ) -> dict[str, str | int] | None:
        """Why a BACK decided on a screen of the app must not go to `focus`
        (the window that takes it, owned by `package`), or None. `fresh` is the
        app's uid read by the gate before its reads of the focus (None when it
        could not be read; unused with the tap window check off). The details
        are those of a tap refused by `uncovered`: ``unreadable`` is
        ``target_user`` or ``target_uid``.

        As for a tap, the focused window must be of Android user 0, and its
        owner uid, when read, the app's. With ``Policy.check_tap_window`` on
        the app's uid must also have been read at the start and read again
        before the gate's reads with the same value (the tap path's rule:
        fail closed; a read that failed counts as not readable). With it
        off the app's uid is never read, as for taps, so only the user is
        checked. The owner package is compared by the caller, so the title
        rule for shared and system uids (which tells packages apart in the
        window list) is not needed here.
        """
        if focus is None:
            return None  # refused as unreadable by the caller
        if focus.user != 0:
            return {"unreadable": "target_user"}
        if not self.policy.check_tap_window:
            return None
        app = self.app_uid if package == self.package else self.also_uids.get(package)
        if (
            app is None
            or (focus.owner_uid is not None and focus.owner_uid != app.uid)
            or fresh != app
        ):
            return {"unreadable": "target_uid"}
        return None

    def refused_tap(
        self, tap: Tap, screen: Screen | None, seen: _Seen
    ) -> Observation | None:
        """Record what a refused tap's gate read; return it as the next
        observation, or None to read the screen again.

        The gate's reads began after its own pin check. If something happened
        to the pin during them they are mixed: they are not recorded, and the
        screen is read again from scratch.
        """
        if self.device.pin_epoch != self.gate_epoch:
            # The skip rows come from the screen the tap was chosen on, now
            # and when the engine releases the tap.
            self.write_skips(screen, tap=tap)
            self.held = (tap, screen)
            return None
        self.write_skips(screen, seen.observation.screen, tap=tap)
        self.record(seen, self.gate_epoch)
        if self.device.pin_epoch != self.gate_epoch:
            return None  # the screenshot's reads saw an event
        self.epoch_seen = self.gate_epoch
        return seen.observation

    def gate(
        self, action: Action, observation: Observation | None
    ) -> tuple[str | None, _Seen | None]:
        """Decide whether `action` may be sent now; return (reason, what was read).

        The one place every action passes right before it is sent. First `stop`
        and the time limit, then that the pinned transport still leads to the
        checked device (an error if not), then `stop` again (the pin check can
        wait for the device), then a fresh read: for a tap the dump
        first and then foreground, focused window and keyboard, so the reads
        nearest the press come last; for a BACK the foreground, the keyboard
        and last the focused window (the window that takes the key). Anything
        that cannot be read, or that differs from what the action was chosen
        on, is a refusal. After the decision nothing else runs before the
        input is sent.
        """
        now = self.now()  # the one time taken, before any read
        reason = self.halt(now)
        if reason is not None:
            return reason, None
        self.device.check_pin(emulator=self.emulator)
        if isinstance(action, Launch):
            return (self.restart_gate() if self.restart else self.launch_gate()), None
        # Asked again after the pin check, which can block for long (a wait
        # for the device); after it only the reads run before the input.
        if self.stopped():
            return NOT_SENT_STOPPED, None
        self.gate_epoch = self.device.pin_epoch
        tap = isinstance(action, Tap)
        fresh = self.back_uid(action, observation)
        # For a tap: the focused window before and after the dump must be the
        # same window of the app, so the dump shows that window (uiautomator
        # dumps the focused window: seen on API 24, 30 and 36 with the shade).
        before = self.device.focused_window() if tap else None
        screen = self.dump() if tap else None
        foreground, focus, keyboard = self.read_state(action)
        self.gate_details = None
        self.gate_focus_details = None
        # With several displays the focus reading is best effort against
        # window titles that forge lines, so the check of the window at the
        # tap point is the guard: without it, a tap or BACK is not sent. Any
        # read of this device in the run that showed several displays counts
        # (the observation's too), and this reason comes before the others.
        guard = isinstance(action, Tap | Back) and not self.policy.check_tap_window
        unchecked = guard and displays_seen(self.device)
        if unchecked:
            before = focus = None
            self.gate_focus_details = {"cause": FOCUS_CAUSE_SEVERAL_DISPLAYS}
        seen = _Seen(Observation(screen, foreground, keyboard, now), focus)
        if isinstance(action, Tap):
            # The last read: the window that takes a touch at the point.
            cover = (
                self.cover(action.point, (before, focus), screen)
                if self.policy.check_tap_window
                else None
            )
            allowed = self.engine.recheck(action, seen.observation)
            owners = {None if f is None else f.owner for f in (before, focus)}
            package = self.tap_package(screen)
            checks = (
                (unchecked, NOT_SENT_FOCUS_UNKNOWN),
                (keyboard is None, NOT_SENT_KEYBOARD_UNKNOWN),
                (keyboard is True, NOT_SENT_KEYBOARD_SHOWN),
                (before is None or focus is None, NOT_SENT_FOCUS_UNKNOWN),
                (owners != {package}, NOT_SENT_FOCUS_OTHER),
                (before != focus, NOT_SENT_FOCUS_CHANGED),
                (
                    cover is not None,
                    NOT_SENT_COVER_UNKNOWN if cover is None else cover[0],
                ),
                (not allowed, NOT_SENT_REFUSED),
            )
            if cover is not None:
                self.gate_details = cover[1]
        else:
            before = observation
            back = isinstance(action, Back)
            # A BACK goes to the window with input focus. One decided on a
            # screen of the app (the keyboard BACK, a navigation BACK) is sent
            # only while the app owns that window, in Android user 0 (as for
            # a tap); one decided on a screen of
            # another app exists only with Policy.back_on_other_screens.
            own_back = (
                back
                and before is not None
                and before.screen is not None
                and self.on_target(before.screen, before.foreground)
            )
            # Only the user and uid of a window the app owns are asked: any
            # other owner is refused by the package check below.
            other = (
                self.back_window(focus, focus.owner, fresh)
                if own_back
                and not unchecked
                and before is not None
                and before.screen is not None
                and focus is not None
                and focus.owner == self.tap_package(before.screen)
                else None
            )
            if other is not None:
                self.gate_focus_details = other
            checks = (
                (unchecked, NOT_SENT_FOCUS_UNKNOWN),
                (before is None, NOT_SENT_CHANGED),  # not reached: a launch only
                (foreground is None, NOT_SENT_FOREGROUND_UNKNOWN),
                (focus is None, NOT_SENT_FOCUS_UNKNOWN),
                (
                    own_back
                    and before is not None
                    and before.screen is not None
                    and focus is not None
                    and focus.owner != self.tap_package(before.screen),
                    NOT_SENT_FOCUS_OTHER,
                ),
                (back and keyboard is None, NOT_SENT_KEYBOARD_UNKNOWN),
                (
                    before is not None
                    and (
                        foreground != before.foreground
                        or focus != self.focus
                        or (back and keyboard != before.keyboard_shown)
                    ),
                    NOT_SENT_CHANGED,
                ),
                # Last of the checks of this branch: every existing reason wins.
                (other is not None, NOT_SENT_FOCUS_OTHER),
            )
        # The engine re-checks with the same time the gate took first, which was
        # before the limit, so its refusal is never about the time.
        # Last, after every other check: if anything happened to the pin since
        # the observation the action was chosen on (or during these reads),
        # nothing read before can be trusted.
        # Every condition is evaluated to a reason first (the truth tests run
        # here), then nothing but one integer comparison follows.
        why = next((reason for failed, reason in checks if bool(failed)), None)
        self.gate_screen_id = None if screen is None else screen.screen_id
        # The time again, after the reads (they can take long) and before the
        # last comparison, as on main: the clock, which may be the caller's
        # code, runs before the comparison and nothing runs after it. The
        # device also checks the time with uiprobe's own clock right before
        # the input goes out.
        late = self.now() >= self.deadline
        # uiprobe's own check once more, after the caller's clock (which may
        # have read the device and shown several displays first) and before
        # the final comparison; its cause wins over the reasons found so far
        # (a changed pin or the time limit below still come first).
        if guard and displays_seen(self.device):
            self.gate_focus_details = {"cause": FOCUS_CAUSE_SEVERAL_DISPLAYS}
            why = NOT_SENT_FOCUS_UNKNOWN
        path = (
            "tap"
            if isinstance(action, Tap)
            else "back"
            if isinstance(action, Back)
            else "force-stop"
        )
        if not self.final_check(path, self.epoch_seen):
            return NOT_SENT_RECONNECTED, seen
        if late:
            return NOT_SENT_TIME_LIMIT, None
        return why, seen

    def launch_gate(self) -> str | None:
        """The end of the first launch's gate, after the pin check: `stop`
        again (the check can take long), the launcher read, the time again
        (the read can take long too), then the last comparison."""
        epoch = self.device.pin_epoch
        stopped = self.stopped()  # asked again after the pin check
        read = stopped or self.read_start_component(None)
        now = self.now()
        late = now >= self.deadline or self.past()
        if not read or not self.final_check("launch", epoch):
            return NOT_SENT_RECONNECTED
        if stopped:
            return NOT_SENT_STOPPED
        if late:
            return NOT_SENT_TIME_LIMIT
        return None

    def restart_gate(self) -> str | None:
        """The end of the gate of the first launch's force-stop, after the
        pin check: `stop` again (the check can take long), the launcher read,
        the time again (the read can take long too), then the last
        comparison, as in `launch_gate`.

        It is the gate of a relaunch's force-stop without the reads of the
        screen, which compare it with the observation it was chosen on: the
        first launch has none. The launcher activity is read here so that an
        app whose launcher cannot be read is not force-stopped: nothing is
        sent, and the run ends as a first launch without the force-stop does
        (`read_start_component`). The start does not use this read: the
        launcher activity is read again after the force-stop, right before
        the start, as for a relaunch.
        """
        epoch = self.device.pin_epoch
        stopped = self.stopped()  # asked again after the pin check
        read = stopped or self.read_start_component(None)
        self.start_component = None  # the start uses the read after the stop
        now = self.now()
        late = now >= self.deadline or self.past()
        if not read or not self.final_check("force-stop", epoch):
            return NOT_SENT_RECONNECTED
        if stopped:
            return NOT_SENT_STOPPED
        if late:
            return NOT_SENT_TIME_LIMIT
        return None

    def read_start_component(self, relaunch_id: int | None) -> bool:
        """Read the launcher activity for the start about to go out; False
        when the device had to be proven again during the read.

        When it cannot be read, the start is not sent: the action is closed
        (a first launch as not sent, a relaunch with `relaunch_id` as
        ``start_not_sent``) and `LaunchFailedError` (``launcher_unreadable``)
        ends the run. Any other error of uiprobe or of the device in the read
        of a first launch (`relaunch_id` None; an adb connection lost, a
        changed device) also closes it as not sent, with the error's word
        (`_failure_word`, ``adb_error`` for a lost connection), before it is
        raised: nothing was sent. In a relaunch, `relaunch` closes it.
        A wait for the device that was stopped (``pin_wait_stopped``) is
        closed by the driver's ending, with the reason that stopped it. When
        writing that end line fails (`RunFolderError`), the error of the read
        is the one raised.
        """
        self.start_component = None
        try:
            read = self.device._start_component  # noqa: SLF001 - internal to uiprobe
            self.start_component = read(self.package)
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == PIN_NOT_PROVEN:
                return False
            if reason != PIN_WAIT_STOPPED and relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        except LaunchFailedError as error:
            if _unreadable_launcher(error):
                if relaunch_id is not None:
                    with contextlib.suppress(RunFolderError):
                        self.half_sent(relaunch_id, "launcher_unreadable")
                else:
                    self.close_unsent("launcher_unreadable")
            elif relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        except UiprobeError as error:
            if relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        return True
