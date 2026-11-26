"""The state of one crawl: `_RunState` sets it in `__init__`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from uiprobe.crawl import CrawlEngine, Tap  # noqa: TC001
from uiprobe.device import _monotonic
from uiprobe.guards import Policy  # noqa: TC001
from uiprobe.runlog import RunWriter  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable

    from uiprobe.crawl import Action, Launch, Limits, Observation, Relaunch
    from uiprobe.crawl_driver.helpers import Progress, _Seen
    from uiprobe.device import Device, FocusedWindow
    from uiprobe.inputwindows import InputWindow
    from uiprobe.model import Screen
    from uiprobe.packageuid import PackageUid


class _RunState:
    """Where every mixin of `_Run` finds the state of one crawl."""

    def __init__(
        self,
        device: Device,
        package: str,
        writer: RunWriter,
        engine: CrawlEngine,
        policy: Policy,
        *,
        emulator: bool,
        app_uid: PackageUid | None = None,
        screenshots: bool,
        settle: float,
        deadline: float,
        stop: Callable[[], bool] | None,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        send_limit: float | None = None,
        own_clock: bool = False,
        restart: bool = False,
        on_progress: Callable[[Progress], None] | None = None,
        started_at: float = 0.0,
        limits: Limits | None = None,
    ) -> None:
        self.device = device
        self.package = package
        self.writer = writer
        self.engine = engine
        self.policy = policy
        self.emulator = emulator
        self.app_uid = app_uid  # read once at the start (check_tap_window)
        # Whether the first launch force-stops the app before it starts it.
        self.restart = restart
        # The launcher activity read in the gate of the start about to go
        # out (the first launch or a relaunch), used by that start only.
        self.start_component: str | None = None
        self.gate_details: dict[str, str | int] | None = None  # why, for cover
        # Why the focus counted as unknown, when not because it was unreadable.
        self.gate_focus_details: dict[str, str | int] | None = None
        self.not_sent: dict[str, int] = {}  # not_sent end lines by reason
        self.cover_unknown: str | None = None  # what the first cover_unknown missed
        self.cover_rule: str | None = None  # and the reader's rule, when it had one
        self.last_screen_id: str | None = None  # the last screen read
        self.last_foreground: str | None = None  # the app in front at the last read
        # Packages in front where the engine acted on another app's screen.
        self.other_apps: dict[str, int] = {}
        self.gate_screen_id: str | None = None  # the id of the gate's own dump
        self.screenshots = screenshots
        self.settle = settle
        self.deadline = deadline
        self.stop = stop
        self.clock = clock
        self.sleep = sleep
        # Called after an action's end line is written (see `report_progress`).
        self.on_progress = on_progress
        self.started_at = started_at  # by `clock`
        self.limits = limits
        self.notes: list[str] = []
        self.screens_seen: set[str] = set()  # target screens, when observed
        self.focus: FocusedWindow | None = None  # at the last observation
        self._stop_seen = False  # stop() once returned True: it stays so
        self._next_id = 0  # the action id of the next start line
        self._open: int | None = None  # sent action that has no end line yet
        # An action whose start line is written and that is in the gate: not
        # sent yet, so a run that ends there can close it as not sent.
        self.unsent: int | None = None
        # A refused tap and the screen it was chosen on, until the engine
        # releases it (see `run`).
        self.held: tuple[Tap, Screen | None] | None = None
        self.withdrawn = False  # the last action was taken back by the engine
        # Device.pin_epoch when the current observation began its reads, and
        # after the gate's own pin check: an input goes out only when nothing
        # happened to the pin since the observation it was chosen on.
        self.epoch_seen = device.pin_epoch
        self.gate_epoch = device.pin_epoch
        self.sent_epoch = device.pin_epoch  # when the last action was sent
        # The run's end as a value of uiprobe's own clock, set once at the
        # start; later values can only come earlier (`now`).
        self.send_limit = (
            _monotonic() + 3600.0 * 24 if send_limit is None else send_limit
        )
        # The time limit as a value of uiprobe's own clock (`now`), by
        # which the next input, stop or start must go out.
        self.send_by: float = self.send_limit
        # Whether `clock` is uiprobe's own (the default): then it is read
        # once per check and the end set at the start stays as it is.
        self.own_clock = own_clock
        # The pin epoch the last `final_check` approved; the device compares
        # it again right before the request is written.
        self.approved: int = device.pin_epoch
        self.repins_before = len(device.repins)  # moves made before this run
        # Why a wait for the device was stopped (a not_sent reason), once known.
        self.wait_stop: str | None = None
        self._skips_written = 0

    if TYPE_CHECKING:
        # What the other mixins define, for the type checker only (these
        # lines do not run).
        def final_check(self, path: str, approved: int) -> bool: ...
        def epoch_is(self, approved: int) -> bool: ...
        def cover(
            self,
            point: tuple[int, int],
            focus: tuple[FocusedWindow | None, ...],
            screen: Screen | None,
        ) -> tuple[str, dict[str, str | int]] | None: ...
        def app_window(self, window: InputWindow, app: PackageUid) -> bool: ...
        def uncovered(
            self,
            app: PackageUid,
            focus: tuple[FocusedWindow | None, ...],
            screen: Screen | None,
        ) -> str | None: ...
        def refused_tap(
            self, tap: Tap, screen: Screen | None, seen: _Seen
        ) -> Observation | None: ...
        def gate(
            self, action: Action, observation: Observation | None
        ) -> tuple[str | None, _Seen | None]: ...
        def launch_gate(self) -> str | None: ...
        def restart_gate(self) -> str | None: ...
        def read_start_component(self, relaunch_id: int | None) -> bool: ...
        def send_input(self, action: Action, action_id: int) -> str | None: ...
        def send(
            self, action: Action, observation: Observation | None
        ) -> tuple[str | None, Observation | None]: ...
        def write_start(
            self, action_id: int, action: Action, observation: Observation | None
        ) -> None: ...
        def close_unsent(self, reason: str) -> None: ...
        def launch(self) -> None: ...
        def relaunch(self, action_id: int, action: Launch | Relaunch) -> str | None: ...
        def relaunch_stop(
            self, action_id: int, action: Launch | Relaunch
        ) -> str | None: ...
        def relaunch_halt(self, action_id: int) -> str | None: ...
        def half_sent(self, action_id: int, why: str) -> str: ...
        def dump(self) -> Screen | None: ...
        def observe(self) -> Observation: ...
        def close_transition(
            self, screen: Screen | None, foreground: str | None
        ) -> None: ...
        def record(self, seen: _Seen, epoch: int) -> None: ...
        def on_target(self, screen: Screen, foreground: str | None) -> bool: ...
        def write_skips(
            self, *screens: Screen | None, tap: Tap | None = None
        ) -> None: ...
        def run(self) -> str: ...
        def run_loop(self) -> str: ...
        def first_launch(self) -> str | None: ...
        def next_observation(self) -> Observation | None: ...
        def end_failed(self, action_id: int, error: Exception) -> None: ...
        def end_not_sent(
            self,
            action_id: int,
            reason: str,
            details: dict[str, str | int] | None = None,
        ) -> None: ...
        def withdraw(self, action: Action) -> None: ...
        def ending(self, action: Action, refused: str | None) -> str | None: ...
        def waiting_stopped(self) -> bool: ...
        def report_progress(self, observation: Observation) -> None: ...
        def now(self) -> float: ...
        def past(self) -> bool: ...
        def check_time(self) -> None: ...
        def stopped(self) -> bool: ...
        def count(self) -> tuple[int, int]: ...
        def halt(self, now: float | None = None) -> str | None: ...
        def pause(self) -> None: ...
