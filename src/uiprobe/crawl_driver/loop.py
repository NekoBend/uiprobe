"""The loop of a run, its ends and its clock."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from uiprobe.crawl import RELAUNCH_LIMIT, Back, Observation, Relaunch, Reread, Stop
from uiprobe.crawl_driver.constants import (
    _HALF_RELAUNCH,
    INTERRUPTED,
    MAX_DURATION,
    NOT_SENT_COVER_UNKNOWN,
    NOT_SENT_RECONNECTED,
    NOT_SENT_START,
    NOT_SENT_STOPPED,
    NOT_SENT_TIME_LIMIT,
    PAUSE_STEP,
    STOP_NOT_SENT,
)
from uiprobe.crawl_driver.helpers import Progress, _Ended, _kind, _seconds
from uiprobe.crawl_driver.state import _RunState
from uiprobe.device import _monotonic
from uiprobe.errors import LaunchFailedError, UiprobeError
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from uiprobe.crawl import Action


class _LoopMixin(_RunState):
    """The loop of a run, how it ends, and its clock."""

    # -- the loop --

    def run(self) -> str:
        """Run until a stop reason is known; return it."""
        try:
            return self.run_loop()
        except _Ended as ended:
            if self.unsent is not None:  # in the gate: not sent
                why = (
                    NOT_SENT_TIME_LIMIT
                    if ended.reason == MAX_DURATION
                    else NOT_SENT_STOPPED
                )
                self.end_not_sent(self.unsent, why)
                self.unsent = None
            return ended.reason

    def run_loop(self) -> str:
        """`run`, which a stop seen inside an observation ends."""
        reason = self.first_launch()
        if reason is not None:
            return reason
        engine = self.engine
        observation = self.next_observation()
        if observation is not None:
            self.report_progress()  # the first launch is sent and closed
        while observation is not None:
            if self.stopped():
                return INTERRUPTED
            action = engine.next(observation)
            # A tap refused on reads an event crossed is released here: its
            # rows name elements of the screen it was chosen on.
            held, self.held = self.held, None
            if held is None:
                self.write_skips(observation.screen)
            else:
                self.write_skips(observation.screen, held[1], tap=held[0])
            if isinstance(action, Stop):
                if action.reason == RELAUNCH_LIMIT:
                    self.note_landing(observation)
                return action.reason
            if isinstance(action, Reread):
                observation = self.observe()
                continue
            refused, fresh = self.send(action, observation)
            if refused != NOT_SENT_RECONNECTED and isinstance(action, Back | Relaunch):
                self.note_landing(observation)  # a taken-back action is read again
            ending = self.ending(action, refused)
            if ending is not None:
                return ending
            # A tap the gate did not send: the screen it re-read goes to the
            # engine, which releases the unreported tap.
            observation = fresh if fresh is not None else self.next_observation()
            if observation is not None and refused is None:
                self.report_progress()
        return INTERRUPTED

    def report_progress(self) -> None:
        """Tell `on_progress` where the run is, after a sent action's end line.

        Called from the loop only: the end line of the action is written, and
        the next send is a whole gate away, so the caller's code never runs
        between a gate's last comparison and its input. It is not called for
        an action that was not sent, or once the run has stopped (the loop
        calls it only with a screen read in hand). An error of the callback
        ends the run like any other error of the loop; it is not caught here.
        """
        callback, limits = self.on_progress, self.limits
        if callback is None or limits is None:
            return
        callback(
            Progress(
                elapsed=max(0.0, self.now() - self.started_at),
                actions=self.engine.actions_sent,
                action_limit=limits.actions,
                screens=len(self.screens_seen),
                depth=max(1, self.engine.depth),
                depth_limit=limits.depth,
                minutes=limits.minutes,
            )
        )

    def note_landing(self, observation: Observation) -> None:
        """Count the app in front when the engine acted on another app's screen.

        The engine's own rule decides (the dump and the foreground both name
        the target app); a foreground that is unknown or is the target itself
        names no other app.
        """
        screen, front = observation.screen, observation.foreground
        if (
            screen is not None
            and front is not None
            and front != self.package
            and not self.on_target(screen, front)
        ):
            self.other_apps[front] = self.other_apps.get(front, 0) + 1

    def first_launch(self) -> str | None:
        """Send the first launch; the stop reason when the run ends there."""
        if self.stopped():
            return INTERRUPTED
        refused, _ = self.send(self.engine.start(), None)
        if refused == NOT_SENT_RECONNECTED:
            self.notes.append(f"launch was not sent: {refused}")
            return STOP_NOT_SENT
        if refused == _HALF_RELAUNCH:
            return STOP_NOT_SENT  # the summary already says what happened
        if refused is not None:  # otherwise only stop() or the time limit
            return INTERRUPTED if self._stop_seen else MAX_DURATION
        return None

    def next_observation(self) -> Observation | None:
        """Ask `stop` again, then read the screen; None when `stop` said so
        (after an action, before any read of the screen)."""
        if self.stopped():
            return None
        return self.observe()

    def end_failed(self, action_id: int, error: Exception) -> None:
        """Close a launch or relaunch whose sending failed and ends the run.

        The end line has ``failed``, the reason and ``outcome: unknown``: the
        device may have acted on what was sent. A start whose reply did not
        come in time also gets a note in the summary.
        """
        self._open = None
        if isinstance(error, LaunchFailedError):
            reason = str(error.details.get("reason", error.code))
        elif isinstance(error, UiprobeError):
            reason = error.code
        else:
            reason = "internal_error"
        if reason == "start_timeout":
            seconds = (
                error.details.get("timeout")
                if isinstance(error, UiprobeError)
                else None
            )
            self.notes.append(
                f"the reply of am start -W for {self.package} did not come within"
                f" {seconds} seconds; whether the app started is unknown, so the"
                " run ended"
            )
        with contextlib.suppress(RunFolderError):
            self.writer.write_transition_end(action_id, failed=reason)

    def end_not_sent(
        self,
        action_id: int,
        reason: str,
        details: dict[str, str | int] | None = None,
    ) -> None:
        """Write a not_sent end line and count it for the result."""
        self.writer.write_transition_end(action_id, not_sent=reason, details=details)
        self.not_sent[reason] = self.not_sent.get(reason, 0) + 1
        if reason == NOT_SENT_COVER_UNKNOWN and details and self.cover_unknown is None:
            self.cover_unknown = str(details.get("unreadable", ""))
            rule = details.get("rule")
            self.cover_rule = None if rule is None else str(rule)

    def withdraw(self, action: Action) -> None:
        """Have the engine take back `action`, which was not sent."""
        self.engine.withdraw(action)
        self.withdrawn = True
        # Skip rows written for the undone call stay written; the writer
        # drops repeats when the engine reports them again.
        self._skips_written = min(self._skips_written, len(self.engine.records.skipped))

    def ending(self, action: Action, refused: str | None) -> str | None:
        """The stop reason when the run ends after `action`, or None to go on.

        Asked to stop or out of time (also when stop() said so in the pause
        after the input, whose start line then has no end line: its result is
        unknown, as the run folder page says): end now, without another
        request. A BACK or relaunch the gate held back: the engine already
        counted it, so rather than go on from a wrong count the run ends
        (alpha: the safe side).
        """
        if self.withdrawn:
            # Taken back by the engine: the crawl goes on from a fresh read.
            self.withdrawn = False
            return INTERRUPTED if self._stop_seen else None
        if refused == _HALF_RELAUNCH:
            return STOP_NOT_SENT  # the summary already says what happened
        if refused in (NOT_SENT_STOPPED, NOT_SENT_TIME_LIMIT, NOT_SENT_START):
            return INTERRUPTED if self._stop_seen else MAX_DURATION
        if refused is not None and isinstance(action, Back | Relaunch):
            self.notes.append(f"{_kind(action)} was not sent: {refused}")
            return STOP_NOT_SENT
        return INTERRUPTED if self._stop_seen else None

    def waiting_stopped(self) -> bool:
        """Whether a wait for the device should end: the time limit, or a
        `stop` that already returned True before the wait. `stop` itself is
        not called while the device is being proven (it may read the device,
        which is refused then); it is asked again after the wait. The first
        reason is kept for the ending."""
        if self.wait_stop is None:
            if self._stop_seen:
                self.wait_stop = NOT_SENT_STOPPED
            elif self.now() >= self.deadline:
                self.wait_stop = NOT_SENT_TIME_LIMIT
        return self.wait_stop is not None

    def now(self) -> float:
        """The run's clock, read once and kept as a built-in float.

        With uiprobe's own clock (the default) it is read once, and the run's
        end by it (`send_by`, which the device checks right before a request
        that changes it goes out) is the one set at the start. With a clock
        of the caller's, uiprobe's own clock is read first, and the time the
        caller's clock says is left, counted from that moment, can bring
        `send_by` earlier, never later.
        """
        if self.own_clock:
            return _monotonic()
        before = _monotonic()
        now = _seconds(self.clock())
        self.send_by = min(self.send_by, before + (self.deadline - now))
        return now

    def past(self) -> bool:
        """Whether the run's end by uiprobe's own clock has come."""
        return _monotonic() >= self.send_by

    def check_time(self) -> None:
        """End the run (``max_duration``) once its end by uiprobe's own clock
        has come (asked when an observation begins)."""
        if self.past():
            raise _Ended(MAX_DURATION)

    def stopped(self) -> bool:
        """Whether `stop` asked to end; once it did, the answer stays True."""
        if not self._stop_seen and self.stop is not None and bool(self.stop()):
            self._stop_seen = True
        return self._stop_seen

    def count(self) -> tuple[int, int]:
        """The target screens observed and the start lines on disk."""
        return len(self.screens_seen), self.writer.starts_written

    def halt(self, now: float | None = None) -> str | None:
        """The reason not to send that does not depend on the screen, if any."""
        if self.stopped():
            return NOT_SENT_STOPPED
        if (self.now() if now is None else now) >= self.deadline:
            return NOT_SENT_TIME_LIMIT
        return None

    def pause(self) -> None:
        """Wait `settle` seconds, but not past the time limit or a stop.

        The number of steps is bounded, so a clock that does not move cannot
        keep the pause going.
        """
        end = min(self.now() + self.settle, self.deadline)
        for _ in range(int(self.settle / PAUSE_STEP) + 2):
            left = end - self.now()
            if left <= 0 or self.stopped():
                return
            self.sleep(min(PAUSE_STEP, left))
