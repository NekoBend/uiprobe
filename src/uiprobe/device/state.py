"""`_DeviceState`: the state of one `Device`, set in `__init__`."""

from __future__ import annotations

# Imported at run time, not only for type checking, so that
# typing.get_type_hints() resolves the annotations of Device's methods.
import os  # noqa: TC003
import time
from typing import TYPE_CHECKING

from uiprobe.device.select import (
    DeviceIdentity,
    _check_serial,
    _Dispatch,
    _make_client,
    select_device,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from uiprobe.adb import DeviceInfo
    from uiprobe.device.apps import DEFAULT_LAUNCH_TIMEOUT
    from uiprobe.device.focus import FocusedWindow
    from uiprobe.device.gestures import DEFAULT_PRESS_MS, DEFAULT_SWIPE_MS
    from uiprobe.device.keys import RefusedKeys
    from uiprobe.errors import DeviceNotFoundError, LaunchFailedError, UiprobeError
    from uiprobe.inputwindows import InputWindow
    from uiprobe.model import Element, Screen
    from uiprobe.packageuid import PackageUid
    from uiprobe.windowdiag import TapWindowDiagnosis


class _DeviceState:
    """Where every mixin of `Device` finds the state of one device."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb_server: str | None = None,
        adb: str | os.PathLike[str] | None = None,
        start_server: bool = True,
        pin: bool = False,
    ) -> None:
        if isinstance(serial, str):
            serial = str.__str__(serial)  # a plain str from here on
        _check_serial(serial)
        if not isinstance(pin, bool):
            msg = f"pin must be a bool, not {type(pin).__name__}"
            raise TypeError(msg)
        self._client = _make_client(adb_server, adb, start_server=start_server)
        self._info = select_device(self._client.devices(), serial)
        self._api_level: int | None = None
        self._api_level_known = False
        self._several_seen = False  # see _displays_seen
        # The latest dump with several displays named another focused display.
        self._other_display = False
        self._latest_screen: Screen | None = None
        # True when the latest screen's display came from `wm size`, False when
        # it is the extent of the dumped windows; always set with the screen.
        self._latest_display_measured = False
        # The clock and the pause of `launch`; tests replace them.
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        # Set by `pin_transport`: every later request uses this transport, and
        # the identity read through it is what the device must keep.
        self._transport_id: int | None = None
        self._pinned_identity: DeviceIdentity | None = None
        self._repins: list[tuple[int, int]] = []
        self._pin_watch: Callable[[], bool] | None = None
        self._repinning = False  # inside a re-pin: no second one
        self._reconnects = 0  # times the device had to be proven again
        # Moves on every event that makes earlier reads doubtful: a failed
        # request on the pin, a wait, a move, a failed pin. `_proven` is its
        # value when the identity was last proven on the current number.
        self._epoch = 0
        self._proven = 0
        self._broken: tuple[str, dict[str, object]] | None = None  # failed pin
        # Requests that did not succeed, pinned or not; and how many launches
        # are past their first request (no adb server is started for them).
        self._failed_requests = 0
        self._launch_holds = 0
        if pin:
            # Right after the listing, so the identity is the chosen device's.
            self.pin_transport()

    if TYPE_CHECKING:
        # What the mixins define, for the type checker only (these lines do not
        # run).
        @property
        def serial(self) -> str: ...
        @property
        def transport_id(self) -> int | None: ...
        def _pin(self, *, strict: bool = False) -> dict[str, int]: ...
        def _call[T](
            self,
            send: Callable[..., T],
            *,
            strict: bool,
            changing: bool = False,
            dispatch: _Dispatch | None = None,
        ) -> T: ...
        def _time_up(self) -> DeviceNotFoundError: ...
        def _dispatch_guard(
            self, epoch: int, dispatch: _Dispatch | None
        ) -> Callable[[], None]: ...
        def _check_listed(self, *, strict: bool = False) -> None: ...
        def _changed(
            self, why: str = "does not lead to that device any more"
        ) -> DeviceNotFoundError: ...
        def _fail(self, error: DeviceNotFoundError) -> DeviceNotFoundError: ...
        def _doubt[E: Exception](self, failure: E) -> E: ...
        def _interrupted(self, error: BaseException) -> None: ...
        def _broken_error(self) -> DeviceNotFoundError: ...
        def _same_device(self, found: DeviceIdentity | None) -> bool: ...
        def _stop_if_watched(self) -> None: ...
        def _count_reconnect(self) -> None: ...
        def _repin(self) -> None: ...
        def _candidate(self, old: int) -> int | None: ...
        def _move(self, old: int, new: int) -> None: ...
        def _identity_through(self, transport: int) -> DeviceIdentity | None: ...
        def _first_proof(self) -> tuple[int, DeviceIdentity]: ...
        def pin_transport(self) -> int: ...
        @property
        def pinned_identity(self) -> DeviceIdentity | None: ...
        @property
        def can_repin(self) -> bool: ...
        @property
        def repins(self) -> tuple[tuple[int, int], ...]: ...
        @property
        def reconnects(self) -> int: ...
        @property
        def pin_epoch(self) -> int: ...
        @property
        def pin_watch(self) -> Callable[[], bool] | None: ...
        def set_pin_watch(self, watch: Callable[[], bool] | None) -> None: ...
        @property
        def info(self) -> DeviceInfo: ...
        def _shell(self, command: str) -> str: ...
        def _extras(self, guard: Callable[[], None] | None) -> dict[str, object]: ...
        def _shell_text(
            self,
            command: str,
            *,
            timeout: float | None = None,
            strict: bool = False,
            dispatch: _Dispatch | None = None,
        ) -> str: ...
        def api_level(self) -> int | None: ...
        def is_emulator(self) -> bool: ...
        def package_uid(self, package: str) -> PackageUid | None: ...
        def keyboard_shown(self) -> bool | None: ...
        def dump(self, *, details: bool = True) -> Screen: ...
        def _dump_xml(self) -> bytes: ...
        def _run_uiautomator(self, path: str) -> str: ...
        def _display_size(self, rotation: int) -> tuple[int, int] | None: ...
        def _note_dump(self, lines: list[str]) -> bool: ...
        def foreground(self) -> str | None: ...
        def focused_window(self) -> FocusedWindow | None: ...
        def identity(self) -> DeviceIdentity | None: ...
        def check_pin(self, *, emulator: bool) -> None: ...
        def window_at(
            self,
            point: tuple[int, int],
            *,
            owned: Callable[[InputWindow], bool] | None = None,
        ) -> InputWindow | str: ...
        def diagnose_tap_window(
            self,
            *,
            show_device_text: bool = False,
            owned: Callable[[InputWindow], bool] | None = None,
        ) -> TapWindowDiagnosis: ...
        def _trusted_component(self) -> tuple[str, str] | None: ...
        def _focused_component(self, package: str) -> tuple[str, str] | None: ...
        def tap(
            self,
            target: Element | tuple[int, int] | list[int],
            *,
            long: bool = False,
            press_ms: int = DEFAULT_PRESS_MS,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> tuple[int, int]: ...
        def _wheel_scroll(
            self,
            point: tuple[int, int],
            amount: int,
            *,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> tuple[int, int]: ...
        def _wheel_supported(self) -> bool: ...
        def swipe(
            self,
            start: tuple[int, int] | list[int],
            end: tuple[int, int] | list[int],
            *,
            duration_ms: int = DEFAULT_SWIPE_MS,
        ) -> tuple[tuple[int, int], tuple[int, int]]: ...
        def scroll(
            self,
            direction: str,
            *,
            within: Element | None = None,
            duration_ms: int = DEFAULT_SWIPE_MS,
        ) -> tuple[tuple[int, int], tuple[int, int]]: ...
        def scroll_display(
            self, direction: str, *, duration_ms: int = DEFAULT_SWIPE_MS
        ) -> tuple[tuple[int, int], tuple[int, int]]: ...
        def key(
            self,
            name: str,
            *,
            refused: RefusedKeys | None = None,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> str: ...
        def type_text(self, text: str) -> None: ...
        def _type_piece(
            self, piece: str, number: int, pieces: int
        ) -> UiprobeError | None: ...
        def launch(
            self,
            package: str,
            *,
            restart: bool = False,
            timeout: float = DEFAULT_LAUNCH_TIMEOUT,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> None: ...
        def _launch(
            self,
            package: str,
            limit: float,
            *,
            restart: bool,
            dispatch: _Dispatch | None,
            activity: str | None = None,
        ) -> None: ...
        def _launch_component(
            self,
            package: str,
            activity: str,
            *,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> None: ...
        def _start_component(self, package: str) -> str: ...
        def _start_component_after_stop(self, package: str) -> str: ...
        def _check_no_failure(self, failed: int) -> None: ...
        def _launch_steps(
            self,
            package: str,
            limit: float,
            *,
            restart: bool,
            dispatch: _Dispatch | None,
            activity: str | None = None,
        ) -> None: ...
        def launcher_activity(self, package: str) -> str: ...
        def _launcher_activity(self, package: str, *, strict: bool = False) -> str: ...
        def _start(
            self,
            package: str,
            activity: str,
            limit: float,
            *,
            force_stopped: bool,
            dispatch: _Dispatch | None = None,
        ) -> LaunchFailedError | None: ...
        def stop_app(
            self,
            package: str,
            *,
            _send_by: float | None = None,
            _epoch: int | None = None,
        ) -> None: ...
        def _force_stop(
            self, package: str, dispatch: _Dispatch | None = None
        ) -> None: ...
        def screenshot(self) -> bytes: ...
        def display_for_image(
            self, width: int, height: int
        ) -> tuple[int, int] | None: ...
