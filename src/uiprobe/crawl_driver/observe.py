"""Reading the screen and recording what the engine decides."""

from __future__ import annotations

from typing import TYPE_CHECKING

from uiprobe.crawl import Observation, Tap, _ids_unique
from uiprobe.crawl_driver.constants import INTERRUPTED
from uiprobe.crawl_driver.helpers import (
    _Ended,
    _hide_password_notes,
    _Seen,
    _skipped_element,
)
from uiprobe.crawl_driver.state import _RunState
from uiprobe.errors import DumpError, ScreenshotFailedError
from uiprobe.guards import _WORD_REASONS

if TYPE_CHECKING:
    from uiprobe.model import Element, Screen


class _ObserveMixin(_RunState):
    """Reading the screen and recording the transitions."""

    def dump(self) -> Screen | None:
        try:
            screen: Screen | None = self.device.dump()
        except DumpError:
            return None
        if screen is not None and not _ids_unique(screen):
            # The engine counts a screen whose element ids are not unique as
            # a failed dump (judge refuses it), so it is recorded as one.
            return None
        return screen

    def observe(self) -> Observation:
        """Read the device, close the open transition, then record the screen.

        The pin is checked first: a dump writes a file on the device and
        removes it, so even reading goes only to the device that was checked.
        Reads that an event on the pin crossed are read again from the pin
        check on (bounded: every event makes the next check prove the device
        again, which `MAX_RECONNECTS` limits), before the screen is written
        or handed on. The open transition is closed as soon as one dump and
        foreground read are not crossed by an event (or the dump failed,
        which the engine counts toward its limit). A failed dump is handed
        on as it is, and `stop` or the time limit end the rereading.
        """
        # A step boundary: `stop` and the run's end are looked at when an
        # observation begins (as before each send); a read in progress then
        # runs until it finishes.
        if self.stopped():
            raise _Ended(INTERRUPTED)
        self.check_time()
        while True:
            self.device.check_pin(emulator=self.emulator)
            self.epoch_seen = self.device.pin_epoch
            screen = self.dump()
            foreground = self.device.foreground()
            if screen is None or self.device.pin_epoch == self.epoch_seen:
                self.close_transition(screen, foreground)
            keyboard = self.device.keyboard_shown()
            focus = self.device.focused_window()
            if (
                screen is None
                or self.device.pin_epoch == self.epoch_seen
                or self.halt() is not None
            ):
                break
        if screen is not None:
            self.last_screen_id = screen.screen_id
        self.last_foreground = foreground
        observation = Observation(screen, foreground, keyboard, self.now())
        self.record(_Seen(observation, focus), self.epoch_seen)
        return observation

    def close_transition(self, screen: Screen | None, foreground: str | None) -> None:
        """Write the end line of the open transition and count the screen."""
        if self._open is not None:
            action_id, self._open = self._open, None
            if screen is None:
                self.writer.write_transition_end(action_id, dump_failed=True)
            else:
                self.writer.write_transition_end(action_id, screen.screen_id)
        if screen is not None and self.on_target(screen, foreground):
            self.screens_seen.add(screen.screen_id)  # counted when observed

    def record(self, seen: _Seen, epoch: int) -> None:
        """Remember the focus, count and write a target screen, then its image.

        Nothing is written when the pin's epoch is no longer `epoch` (the one
        the reads began at): reads that an event crossed may be mixed.
        """
        self.focus = seen.focus
        screen = seen.observation.screen
        if screen is None or not self.on_target(screen, seen.observation.foreground):
            return
        if self.device.pin_epoch != epoch:
            return
        self.screens_seen.add(screen.screen_id)
        if not self.writer.write_screen(screen) or not self.screenshots:
            return
        try:
            png = self.device.screenshot()
            after = self.device.foreground()
        except ScreenshotFailedError as error:
            reason = error.details.get("reason", "unknown")
            self.notes.append(f"no screenshot of {screen.screen_id}: {reason}")
            return
        if after != screen.package:
            # Another app may have come forward while the image was taken.
            self.notes.append(
                f"no screenshot of {screen.screen_id}: the app was not in front"
            )
            return
        if self.device.pin_epoch != epoch:
            # An event crossed the screenshot's reads: the image is dropped.
            self.notes.append(
                f"no screenshot of {screen.screen_id}: the device was proven again"
            )
            return
        self.writer.write_screenshot(screen.screen_id, png)

    def on_target(self, screen: Screen, foreground: str | None) -> bool:
        # The engine's own rule: both the dump and the foreground name the app
        # (or one of the packages of Policy.also_target).
        return foreground == screen.package and (
            screen.package == self.package or screen.package in self.also
        )

    def write_skips(self, *screens: Screen | None, tap: Tap | None = None) -> None:
        """Write the engine's new skipped elements; `screens` hold the elements.

        `tap`, when given, is the tap the engine re-checked: a row about its
        screen and fingerprint names that very element.
        """
        skipped = self.engine.records.skipped
        new = skipped[self._skips_written :]
        self._skips_written = len(skipped)
        found: dict[str, dict[str, list[tuple[Element, tuple[str, ...]]]]] = {}
        for item in new:
            body = _skipped_element(
                item, screens, found, package=self.package, policy=self.policy, tap=tap
            )
            details = [d.to_dict() for d in item.details if d.reason in _WORD_REASONS]
            holder = next(
                (s for s in screens if s and s.screen_id == item.screen_id), None
            )
            details = _hide_password_notes(details, holder, body)
            self.writer.write_skip(
                item.screen_id, item.fingerprint, item.reasons, body, details
            )
