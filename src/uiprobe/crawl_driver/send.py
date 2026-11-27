"""Sending inputs, starts and relaunches."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from uiprobe.crawl import Back, Launch, Observation, Relaunch, Scroll, Tap
from uiprobe.crawl_driver.constants import (
    _COVER_REASONS,
    _FIRST_ACTION,
    _HALF_RELAUNCH,
    NOT_SENT_FOCUS_OTHER,
    NOT_SENT_FOCUS_UNKNOWN,
    NOT_SENT_RECONNECTED,
    NOT_SENT_START,
    NOT_SENT_STOPPED,
    NOT_SENT_TIME_LIMIT,
)
from uiprobe.crawl_driver.helpers import _element_of, _failure_word, _kind
from uiprobe.crawl_driver.state import _RunState
from uiprobe.device import PIN_NOT_PROVEN, PIN_WAIT_STOPPED, SEND_STOPPED
from uiprobe.errors import DeviceNotFoundError, LaunchFailedError
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from uiprobe.crawl import Action
    from uiprobe.crawl_driver.helpers import _Seen
    from uiprobe.model import Element, Screen


class _SendMixin(_RunState):
    """Sending inputs, starts and relaunches of a run."""

    def send_input(self, action: Action, action_id: int) -> str | None:
        """Send a tap, BACK or the first launch; return why nothing was sent,
        or None when it was sent.

        ``device_reconnected``: the device has to be proven again first (the
        action is closed as the gate would have closed it, and taken back
        unless it is the first launch). ``time_limit``: the time limit had
        passed when the request was about to go out (checked by the device
        with uiprobe's own clock, see `now`);
        the action is closed as not sent and the run ends.
        """
        try:
            if isinstance(action, Tap):
                self.device.tap(
                    action.point, _send_by=self.send_by, _epoch=self.approved
                )
            elif isinstance(action, Scroll):
                self.device._wheel_scroll(  # noqa: SLF001 - internal to uiprobe
                    action.point,
                    action.amount,
                    _send_by=self.send_by,
                    _epoch=self.approved,
                )
            elif isinstance(action, Back):
                self.device.key("back", _send_by=self.send_by, _epoch=self.approved)
            else:
                self.launch()
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == SEND_STOPPED:
                self._open = None
                why = str(error.details.get("why", NOT_SENT_TIME_LIMIT))
                self.end_not_sent(action_id, why)
                return why
            if reason != PIN_NOT_PROVEN:
                raise
            self._open = None
            self.end_not_sent(action_id, NOT_SENT_RECONNECTED)
            if not isinstance(action, Launch):
                self.withdraw(action)
            return NOT_SENT_RECONNECTED
        return None

    # -- sending --

    def send(
        self, action: Action, observation: Observation | None
    ) -> tuple[str | None, Observation | None]:
        """Write the start line, pass the gate, then send the action once.

        Returns the reason the gate gave when it refused (None when the action
        was sent) and, for a refused tap, the observation it re-read.
        """
        self._next_id += 1
        action_id = self._next_id
        screen = None if observation is None else observation.screen
        self.write_start(action_id, action, observation)
        self.unsent = action_id
        reason, seen = self.gate(action, observation)
        self.unsent = None
        if seen is not None and reason != NOT_SENT_RECONNECTED:
            # A read the pin check kept: it names the last screen and app
            # (the id was computed in the gate, before its last comparison,
            # so no work of this size runs between it and the input).
            if self.gate_screen_id is not None:
                self.last_screen_id = self.gate_screen_id
            self.last_foreground = seen.observation.foreground
        if reason is not None:
            details = (
                self.gate_details
                if reason in _COVER_REASONS
                else self.gate_focus_details
                if reason in {NOT_SENT_FOCUS_UNKNOWN, NOT_SENT_FOCUS_OTHER}
                else None
            )
            self.end_not_sent(action_id, reason, details)
            if reason == NOT_SENT_RECONNECTED and not isinstance(action, Launch):
                # Nothing was sent and nothing about the screen is known: the
                # engine takes the action back and decides again on a fresh
                # read.
                self.withdraw(action)
                return reason, None
            if seen is not None and isinstance(action, Tap | Scroll):
                return reason, self.refused_tap(action, screen, seen)
            return reason, None
        # Nothing of the caller's runs between the gate's last comparison and
        # the input; a start uses the launcher activity its gate read.
        self._open = action_id
        self.sent_epoch = self.device.pin_epoch
        try:
            if isinstance(action, Relaunch) or (
                isinstance(action, Launch) and self.restart
            ):
                return self.relaunch(action_id, action), None
            refused = self.send_input(action, action_id)
        except Exception as error:
            # A launch or relaunch that failed ends the run; its end line says
            # so (a tap or BACK keeps a start line alone: result unknown).
            if isinstance(action, Launch | Relaunch) and self._open == action_id:
                self.end_failed(action_id, error)
            raise
        if refused is not None:
            return refused, None
        self.sent(action, action_id, screen, seen)
        return None, None

    def sent(
        self,
        action: Action,
        action_id: int,
        screen: Screen | None,
        seen: _Seen | None,
    ) -> None:
        """What follows a sent action: tell the engine, then the pause."""
        if isinstance(action, Tap):
            self.engine.tap_sent(action)
            if seen is not None:
                self.write_skips(screen, seen.observation.screen, tap=action)
        elif isinstance(action, Scroll):
            self.engine.scroll_sent(action)
            self.scrolled = action_id
        if isinstance(action, Tap | Scroll | Back):
            self.pause()

    def write_start(
        self, action_id: int, action: Action, observation: Observation | None
    ) -> None:
        """Write the start line of `action`, before its gate."""
        screen = None if observation is None else observation.screen
        from_id = None if screen is None else screen.screen_id
        element: Element | None = None
        point: tuple[int, int] | None = None
        other: str | None = None  # the package of a screen of another app
        foreground: str | None = None
        focus: str | None = None
        if (
            isinstance(action, Back | Relaunch)
            and observation is not None
            and screen is not None
            and not self.on_target(screen, observation.foreground)
        ):
            # Decided on a screen of another app: say whose it was, so the run
            # folder tells where a BACK went or why the app was restarted.
            # The gate sends a BACK only while the focused window is the one
            # read here, so its owner is where that BACK goes.
            other = screen.package
            foreground = observation.foreground
            focus = None if self.focus is None else self.focus.owner
        amount: int | None = None
        if isinstance(action, Tap):
            from_id = action.screen_id
            point = action.point
            element = _element_of(screen, action.element_id)
        elif isinstance(action, Scroll):
            from_id = action.screen_id
            point, amount = action.point, action.amount
        elif isinstance(action, Launch):
            from_id = None
        elif not isinstance(action, Back | Relaunch):  # Stop and Reread
            msg = f"not an input: {action!r}"
            raise TypeError(msg)
        self.writer.write_transition_start(
            action_id,
            _kind(action),
            from_id,
            element,
            point,
            off_target_package=other,
            off_target_foreground=foreground,
            off_target_focus=focus,
            restart=isinstance(action, Launch) and self.restart,
            replay=isinstance(action, Tap) and self.engine.is_replay(action),
            amount=amount,
            after_scroll=self.read_of(action, screen),
            except_phrase=(
                self.engine.except_phrase_of(action) if isinstance(action, Tap) else ""
            ),
        )

    def read_of(self, action: Action, screen: Screen | None) -> int | None:
        """The scroll whose result screen is the screen a tap was decided on
        (the latest written read of that screen id that equals it: the first
        read of an id that a scroll brought is both the id's own file and that
        scroll's read, and it is the scroll's), or None: the tap is of the
        screen's own file, or of a state nothing written holds."""
        if (
            not self.policy.scroll_lists
            or not isinstance(action, Tap)
            or screen is None
        ):
            return None
        for scroll_id, elements in reversed(self.reads.get(action.screen_id, ())):
            if elements == screen.elements:
                return scroll_id
        return None

    def close_unsent(self, reason: str) -> None:
        """Close the action in the gate (if any) as not sent for `reason`.

        Called only while an error is being raised: a failure to write the
        end line (`RunFolderError`) is not raised, so that error stays the
        one raised.
        """
        if self.unsent is not None:
            action_id, self.unsent = self.unsent, None
            with contextlib.suppress(RunFolderError):
                self.end_not_sent(action_id, reason)

    def launch(self) -> None:
        """Start the app; an app that is not in front afterwards is not fatal.

        It starts the launcher activity its gate read (before the gate's last
        clock read and comparison; no component is kept between starts) with
        ``am start -W`` and waits for the app to come to the front. No code of
        the caller runs in that call; the start goes out only while the pin
        epoch is still the one the gate approved and the time limit has not
        passed.
        ``not_in_front`` means the start reported success, but
        another package (a permission dialog, a splash screen) holds the
        focus; ``other_activity`` means the start reported success with an
        activity of another app (on API 36, Settings' search screen of
        another package stays on top of its task after a force-stop) and the
        app was not in front after the same wait either. The
        engine handles an off-target screen, so the crawl observes and goes
        on. The action stays counted as sent. Any other launch failure,
        ``start_timeout`` included (whether the app started is unknown), is
        raised and ends the run.
        """
        component, self.start_component = self.start_component, None
        if component is None:  # every gate of a start reads it first
            msg = "no launcher activity was read for this start"
            raise RuntimeError(msg)
        try:
            self.device._launch_component(  # noqa: SLF001 - internal to uiprobe
                self.package, component, _send_by=self.send_by, _epoch=self.approved
            )
        except LaunchFailedError as error:
            reason = error.details.get("reason")
            if reason == "other_activity":
                # The start went to an activity of another app (one that was
                # on top of the app's task): that app is in front.
                seen = str(error.details.get("activity", "")).partition("/")[0]
            elif reason == "not_in_front":
                seen = error.details.get("foreground")
            else:
                raise
            self.notes.append(f"{self.package} was not in front after launch ({seen})")

    def relaunch(self, action_id: int, action: Launch | Relaunch) -> str | None:
        """Force-stop the app, then, unless told to stop meanwhile, start it.

        A relaunch, and the first launch when `restart` is set. The two
        commands are sent one after the other, and `stop` and the time
        limit are looked at again between them. When they end the run there,
        only the force-stop was sent: the start line gets a ``not_sent`` end
        (``start_not_sent``) and the summary says so. Returns that reason, or
        None when the app was started.
        """
        unsent = self.relaunch_stop(action_id, action)
        if unsent is not None:
            return unsent
        try:
            reason = self.relaunch_halt(action_id)
            # The last comparison before the start, inside the region: nothing
            # runs after it but leaving the region and the tests below.
            stale = reason == NOT_SENT_RECONNECTED or (
                reason is None and not self.final_check("start", self.sent_epoch)
            )
        except BaseException as error:
            # The force-stop went out and nothing has sent the start: an
            # error (a changed device in the pin check, say) or an interrupt
            # here, up to and in the last comparison, leaves no doubt about
            # the start, so the action is closed as start_not_sent (not as
            # failed, or left open) before it is raised again. A step that
            # already closed it is not repeated. `launch` is outside.
            if self._open == action_id:
                with contextlib.suppress(RunFolderError):
                    self.half_sent(action_id, _failure_word(error))
            raise
        if stale:
            # The device had to be proven again after the force-stop: the
            # start is not sent on the earlier decision.
            return self.half_sent(action_id, NOT_SENT_RECONNECTED)
        if reason is not None:
            self.half_sent(action_id, reason)
            return NOT_SENT_START
        try:
            self.launch()
        except DeviceNotFoundError as error:
            why = error.details.get("reason")
            if why == SEND_STOPPED:
                # The time limit passed right before the start went out.
                self.half_sent(action_id, str(error.details.get("why", "")))
                return NOT_SENT_START
            if why != PIN_NOT_PROVEN:
                raise
            # The force-stop went out, the start did not.
            return self.half_sent(action_id, NOT_SENT_RECONNECTED)
        return None

    def relaunch_stop(self, action_id: int, action: Launch | Relaunch) -> str | None:
        """Send the force-stop of a relaunch or of the first launch; why it
        was not sent, or None. A relaunch not sent because the device had to
        be proven again is taken back by the engine; the first launch is not
        (the run ends, as for a first launch without the force-stop)."""
        try:
            self.device.stop_app(
                self.package, _send_by=self.send_by, _epoch=self.approved
            )
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == SEND_STOPPED:
                self._open = None  # nothing was sent: the time limit had passed
                why = str(error.details.get("why", NOT_SENT_TIME_LIMIT))
                self.end_not_sent(action_id, why)
                return why
            if reason != PIN_NOT_PROVEN:
                raise
            self._open = None  # nothing was sent
            self.end_not_sent(action_id, NOT_SENT_RECONNECTED)
            if isinstance(action, Relaunch):
                self.withdraw(action)
            return NOT_SENT_RECONNECTED
        return None

    def relaunch_halt(self, action_id: int) -> str | None:
        """Between the force-stop and the start: `stop`, the time, the pin
        check, `stop`, the launcher read and the time again; why not to start,
        or None."""
        try:
            # stop() may read the device, so it is inside the handler too.
            reason = self.halt()
            if reason is None:
                # The device behind the pin is checked again before the start,
                # then stop() (the check can block for long) and the time,
                # then, in `relaunch`, the last comparison of the pin.
                self.device.check_pin(emulator=self.emulator)
                if self.stopped():
                    reason = NOT_SENT_STOPPED
                elif not self.read_start_component(action_id):
                    reason = NOT_SENT_RECONNECTED
                elif self.now() >= self.deadline or self.past():
                    reason = NOT_SENT_TIME_LIMIT
        except DeviceNotFoundError as error:
            if error.details.get("reason") != PIN_WAIT_STOPPED:
                raise
            # The time limit (or a stop seen before) ended a wait for the
            # device: the start was not sent, which the record says before
            # the run ends.
            self.half_sent(action_id, self.wait_stop or NOT_SENT_STOPPED)
            raise
        return reason

    def half_sent(self, action_id: int, why: str) -> str:
        """Close a relaunch, or the first launch, whose force-stop went out
        and whose start did not; return `_HALF_RELAUNCH`."""
        self._open = None
        self.end_not_sent(action_id, NOT_SENT_START)
        # The first launch is always the run's first action.
        what = (
            "at the start of the run but not started"
            if action_id == _FIRST_ACTION
            else "but not started again"
        )
        self.notes.append(f"{self.package} was force-stopped {what} ({why})")
        return _HALF_RELAUNCH
