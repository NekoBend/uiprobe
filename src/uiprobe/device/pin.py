"""`_PinMixin`: the pinned connection, its re-pinning, and the calls through it."""

from __future__ import annotations

import time

# Imported at run time, not only for type checking, so that
# typing.get_type_hints() resolves the annotations of Device's methods.
from collections.abc import Callable  # noqa: TC003
from typing import TYPE_CHECKING, Final

from uiprobe.device.select import (
    _OPTIONAL_ERRORS,
    IDENTITY_COMMAND,
    OFFLINE_STATE,
    PIN_NOT_PROVEN,
    READY_STATE,
    DeviceIdentity,
    _Dispatch,
    _parse_identity,
)
from uiprobe.device.state import _DeviceState
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceUnauthorizedError,
    UiprobeError,
)

if TYPE_CHECKING:
    from uiprobe.adb import DeviceInfo

# A pinned transport that the adb server renumbered: how long to wait for the
# device to come back, how often to look, and how many times one Device may
# move its pin.
# Commands that change the device (inputs, starting and stopping apps): their
# request never follows a renumbered transport.
_CHANGING_COMMANDS: Final = ("input ", "monkey ", "am ")


REPIN_WAIT: Final = 10.0


REPIN_POLL: Final = 0.5


MAX_REPINS: Final = 5


# Waits for the pinned device to come back (on its number or a new one) that
# one Device allows; the next one fails, so a device that keeps dropping out
# cannot keep a caller re-reading forever.
MAX_RECONNECTS: Final = 10


# details["reason"] when the wait for a renumbered transport was given up
# because the watch set with `Device.set_pin_watch` said so.
PIN_WAIT_STOPPED: Final = "pin_wait_stopped"


# details["reason"] when a request that changes the device was not sent
# because the time it had to go out by had passed: checked with uiprobe's own
# clock on the selected connection, right before the request is written (the
# crawl's time limit); details["why"] is ``time_limit``.
SEND_STOPPED: Final = "send_stopped"


def _monotonic() -> float:
    """uiprobe's own clock for the last check before a request goes out.

    The crawl turns its time limit into a value of this clock before its last
    reads, so no code of the caller runs between that check and the send.
    """
    return time.monotonic()


class _PinMixin(_DeviceState):
    """The pinned connection, its re-pinning, and the calls that go through it."""

    @property
    def serial(self) -> str:
        """The adb serial of this device."""
        return self._info.serial

    @property
    def transport_id(self) -> int | None:
        """The transport every request uses, once `pin_transport` set it."""
        return self._transport_id

    def _pin(self, *, strict: bool = False) -> dict[str, int]:
        """The client keyword that selects the pinned transport, if any.

        While a transport is pinned, every request first checks with the adb
        server (``host:devices-l``) that the pinned transport id still lists
        this serial, so no request goes to a device that a restarted server
        gave the same number. The instant between that check and the request
        itself remains. See `_check_listed` for what else holds.
        """
        if self._broken is not None:
            raise self._broken_error()
        if self._repinning:
            # A request while the device is being proven (first or again;
            # from the watch, say): nothing goes out, and the proof fails.
            raise self._fail(self._changed("was asked for during a proof"))
        if self._transport_id is None:
            return {}
        succeeded = False
        try:
            self._check_listed(strict=strict)
            succeeded = True
        except BaseException as error:
            self._interrupted(error)
            raise
        finally:
            if not succeeded:
                self._epoch += 1  # whatever stopped the check, the proof is stale
        return {"transport_id": self._transport_id}

    def _call[T](
        self,
        send: Callable[..., T],
        *,
        strict: bool,
        changing: bool = False,
        dispatch: _Dispatch | None = None,
    ) -> T:
        """Send one request with the pin; note a lost transport at once.

        A request on the pinned transport that does not succeed, for any
        reason (the device went offline or away, the server is gone, the
        protocol broke, an interrupt), moves `pin_epoch` before the error
        leaves this method, so a caller
        that swallows the error cannot hide it: the next read proves the
        device again (without a pinned boot id it fails the pin instead) and
        the next input is refused.

        A request that `changing` marks (an input, a start or stop of an app)
        is checked once more on its own connection, after the transport was
        selected and right before the request is written: nothing happened to
        the pin since this method began (`pin_epoch`), and uiprobe's own clock
        has not reached the time it had to go out by. Only uiprobe's code runs
        there, never a caller's.
        """
        pin = self._pin(strict=strict)  # moves the epoch itself on a failure
        guard = self._dispatch_guard(self._epoch, dispatch) if changing else None
        succeeded = False
        try:
            result = send(pin) if guard is None else send(pin, guard)
            succeeded = True
        except BaseException as error:
            if pin:
                self._interrupted(error)
            raise
        finally:
            if not succeeded:
                self._failed_requests += 1  # a launch in progress stops on it
                if pin:
                    self._epoch += 1  # any way out but success, whatever the error
        return result

    def _time_up(self) -> DeviceNotFoundError:
        msg = f"nothing was sent to {self.serial}: the time limit had passed"
        details = {"serial": self.serial, "reason": SEND_STOPPED, "why": "time_limit"}
        return DeviceNotFoundError(msg, details=details)

    def _dispatch_guard(
        self, epoch: int, dispatch: _Dispatch | None
    ) -> Callable[[], None]:
        """The last check of a request that changes the device (see `_call`).

        The epoch compared is the one the caller approved (`dispatch`), else
        the one when the request began.
        """
        send_by = None if dispatch is None else dispatch.send_by
        if dispatch is not None and dispatch.epoch is not None:
            epoch = dispatch.epoch

        def guard() -> None:
            if self._epoch != epoch:
                msg = (
                    f"{self.serial} has to be proven again before an input"
                    f" (transport {self._transport_id}); nothing was sent"
                )
                details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
                raise DeviceNotFoundError(msg, details=details)
            if send_by is not None and _monotonic() >= send_by:
                raise self._time_up()

        return guard

    def _check_listed(self, *, strict: bool = False) -> None:
        """Hold every request to one rule; raise when it does not hold.

        The rule for a request that changes the device (`strict`: an input,
        a start or a stop of an app): it goes out only when the pinned number
        is listed, ready, for this serial, and nothing happened since the
        identity was last proven on that number (`pin_epoch` equals the proven
        epoch: no request failed, no wait began, the pin did not move).
        Otherwise nothing is sent: when a read could prove the device again
        (the cases below), it is refused with ``pin_not_proven`` and the pin
        is kept, so the caller reads again; else it is refused with
        ``device_changed`` and the pin fails.

        A read goes out on the same condition. When the number is gone from
        the listing, still lists this serial but offline, or something
        happened since the last proof, the device is proven again first, by
        waiting for it (`_repin`); that needs a pinned boot id. Without one
        the device cannot be proven again at all (another emulator of the
        same image behind the same number would read the same), so any of
        these fails the pin; when something happened since the last proof,
        it fails before the listing is read, so that an adb server that
        cannot be reached does not leave the pin stale. Anything else fails
        the pin too, and so does a request made while a wait is under way
        (from the watch, say).
        """
        if not self.can_repin and self._epoch != self._proven:
            # No boot id: nothing would tell the pinned device from another
            # emulator of the same image behind the same number, so a device
            # that has to be proven again cannot be.
            raise self._fail(self._changed("cannot be proven again without a boot id"))
        everything = self._client.devices()
        if any(
            d.serial == self.serial and d.state not in (READY_STATE, OFFLINE_STATE)
            for d in everything
        ):
            raise self._fail(self._changed("is not usable any more"))
        if (
            sum(d.serial == self.serial and d.state == READY_STATE for d in everything)
            > 1
        ):
            raise self._fail(self._changed("is listed more than once"))
        listed = [d for d in everything if d.transport_id == self._transport_id]
        same = len(listed) == 1 and listed[0].serial == self.serial
        ready = same and listed[0].state == READY_STATE
        if ready and self._epoch == self._proven:
            return
        if not self.can_repin:
            # Proven and nothing happened since, but the number is not listed
            # and ready (renumbered, offline): no boot id, so no wait.
            raise self._fail(self._changed())
        waitable = ready or not listed or (same and listed[0].state == OFFLINE_STATE)
        if strict:
            if not waitable:
                raise self._fail(self._changed())
            if self._reconnects >= MAX_RECONNECTS:
                # No proof is left to try: nothing will make this pin usable.
                raise self._fail(
                    self._changed(f"was away more than {MAX_RECONNECTS} times")
                )
            # Nothing is sent and the pin stays: the next read proves the
            # device again (the caller decides on that fresh read).
            msg = (
                f"{self.serial} has to be proven again before an input"
                f" (transport {self._transport_id}); nothing was sent"
            )
            details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
            raise DeviceNotFoundError(msg, details=details)
        if not waitable:
            raise self._fail(self._changed())
        self._repin()

    def _changed(
        self, why: str = "does not lead to that device any more"
    ) -> DeviceNotFoundError:
        msg = f"the transport pinned for {self.serial} ({self._transport_id}) {why}"
        details = {"serial": self.serial, "reason": "device_changed"}
        return DeviceNotFoundError(msg, details=details)

    def _fail(self, error: DeviceNotFoundError) -> DeviceNotFoundError:
        """Keep a failed pin, so that no later request goes anywhere.

        A caller may swallow the error (a best-effort read does); every later
        request raises it again instead of reaching a device that came back
        after the pin was given up. Only the message and the details are
        kept, not the exception and its frames.
        """
        if self._broken is None:
            self._broken = (str(error), dict(error.details))
            self._epoch += 1
        return error

    def _doubt[E: Exception](self, failure: E) -> E:
        """Note a request that changed the device but did not report success
        (it may have been cut off): the proof is stale. Return `failure`."""
        if self._transport_id is not None:
            self._epoch += 1
        return failure

    def _interrupted(self, error: BaseException) -> None:
        """Keep the pin as failed when an interrupt (not an error) left a
        pinned exchange: such a Device is not to be used again."""
        if not isinstance(error, Exception) and self._transport_id is not None:
            self._fail(self._changed("was interrupted while it was in use"))

    def _broken_error(self) -> DeviceNotFoundError:
        assert self._broken is not None  # noqa: S101 - only called when kept
        message, details = self._broken
        return DeviceNotFoundError(message, details=dict(details))

    def _same_device(self, found: DeviceIdentity | None) -> bool:
        """Whether `found` is the pinned device.

        A pinned boot id must be read again, the same; without one (only
        `check_pin` gets here then), the three other fields are compared and
        a boot id read later is ignored.
        """
        expected = self._pinned_identity
        if found is None or expected is None:
            return False
        if expected.boot_id:
            return found == expected
        return found._replace(boot_id="") == expected

    def _stop_if_watched(self) -> None:
        watch = self._pin_watch
        stopped = watch is not None and watch()
        if self._broken is not None:
            # A request inside the watch failed the pin (and may have been
            # swallowed there): the wait ends with that failure.
            raise self._broken_error()
        if stopped:
            msg = f"the wait for {self.serial} to come back was stopped"
            details = {"serial": self.serial, "reason": PIN_WAIT_STOPPED}
            raise self._fail(DeviceNotFoundError(msg, details=details))

    def _count_reconnect(self) -> None:
        if self._reconnects >= MAX_RECONNECTS:
            raise self._fail(
                self._changed(f"was away more than {MAX_RECONNECTS} times")
            )
        self._reconnects += 1
        self._epoch += 1

    def _repin(self) -> None:
        """Prove the device again, following it to a new number if needed.

        The adb server can renumber a device that went offline for a moment.
        Until `REPIN_WAIT` seconds after the start (an absolute deadline,
        asked before every listing and every probe), the server's listing is
        read every `REPIN_POLL` seconds. An entry of this serial that is
        neither ready nor offline fails the pin; when exactly one entry is
        ready, the identity is read through that number (not taken from the
        listing) and must be the pinned device's (`_same_device`). The watch
        and the deadline are asked once more, and only then does the pin move
        (a move to another number counts toward `MAX_REPINS`). Anything else,
        any error on the way included, fails the pin, which stays where it
        was. The whole wait, the watch included, cannot start a second one.
        """
        old = self._transport_id
        assert old is not None  # noqa: S101 - only called while pinned
        self._repinning = True
        try:
            self._count_reconnect()
            deadline = self._clock() + REPIN_WAIT
            while self._clock() < deadline:
                self._stop_if_watched()
                if self._clock() >= deadline:
                    break
                new = self._candidate(old)
                if new is not None and self._clock() < deadline:
                    found = self._identity_through(new)
                    if found is None:
                        # A failed or unreadable identity read is waited on,
                        # but counted: a launch in progress ends on it.
                        self._failed_requests += 1
                    else:
                        if not self._same_device(found):
                            raise self._fail(
                                self._changed("came back as another device")
                            )
                        self._stop_if_watched()
                        if self._clock() >= deadline:
                            break
                        self._move(old, new)
                        return
                now = self._clock()
                if now < deadline:
                    self._sleep(min(REPIN_POLL, deadline - now))
            raise self._fail(
                self._changed(f"did not come back within {REPIN_WAIT:g} s")
            )
        except BaseException as error:
            code = (
                error.code if isinstance(error, UiprobeError) else type(error).__name__
            )
            self._fail(self._changed(f"could not be proven again ({code})"))
            raise
        finally:
            self._repinning = False
            if self._proven != self._epoch:
                # Any way out but a proof: kept.
                self._fail(self._changed("could not be proven again"))

    def _candidate(self, old: int) -> int | None:
        """Read the listing once; return the one ready number of this serial,
        or None to keep waiting. An entry that rules the device out fails the
        pin: the old number under another serial, an entry of this serial
        that is neither ready nor offline, two ready entries."""
        everything = self._client.devices()
        if any(d.transport_id == old and d.serial != self.serial for d in everything):
            raise self._fail(self._changed("was given to another device"))
        entries = [d for d in everything if d.serial == self.serial]
        if any(d.state not in (READY_STATE, OFFLINE_STATE) for d in entries):
            raise self._fail(self._changed("is not usable any more"))
        ready = [d for d in entries if d.state == READY_STATE]
        if len(ready) > 1:
            raise self._fail(self._changed("is listed more than once"))
        return ready[0].transport_id if ready else None

    def _move(self, old: int, new: int) -> None:
        if new != old:
            if len(self._repins) >= MAX_REPINS:
                raise self._fail(
                    self._changed(f"was renumbered more than {MAX_REPINS} times")
                )
            self._repins.append((old, new))
            self._epoch += 1
        self._transport_id = new
        self._proven = self._epoch

    def _identity_through(self, transport: int) -> DeviceIdentity | None:
        """The identity read through `transport`, or None when that failed.

        The transport is passed to the one request; the pin itself is not
        touched, so nothing else can go to `transport` before it is proven.
        """
        try:
            raw = self._client.shell(
                self.serial, IDENTITY_COMMAND, transport_id=transport
            )
        except (AdbServerUnavailableError, DeviceUnauthorizedError):
            raise  # no use waiting: the wait fails with it
        except _OPTIONAL_ERRORS:
            self._epoch += 1
            return None
        return _parse_identity(raw.decode("utf-8", errors="replace"))

    def _first_proof(self) -> tuple[int, DeviceIdentity]:
        """The transport the device was chosen on and the identity read
        through it; nothing is published here."""
        transport = self._info.transport_id
        if transport is None:
            msg = (
                f"adb reported no transport id for {self.serial}, so the"
                " requests cannot be kept on the device that was chosen"
            )
            raise AdbError(msg, details={"serial": self.serial})
        listed = [d for d in self._client.devices() if d.transport_id == transport]
        if not (
            len(listed) == 1
            and listed[0].serial == self.serial
            and listed[0].state == READY_STATE
        ):
            msg = (
                f"the transport chosen for {self.serial} ({transport}) does"
                " not lead to that device any more"
            )
            details = {"serial": self.serial, "reason": "device_changed"}
            raise DeviceNotFoundError(msg, details=details)
        found = _parse_identity(
            self._client.shell(
                self.serial, IDENTITY_COMMAND, transport_id=transport
            ).decode("utf-8", errors="replace")
        )
        if found is None or not found.fingerprint:
            msg = (
                f"the identity of {self.serial} (ro.build.fingerprint) could"
                " not be read, so the device cannot be told apart later"
            )
            raise AdbError(msg, details={"serial": self.serial})
        return transport, found

    def pin_transport(self) -> int:
        """Send every later request to the transport this device was chosen on.

        The transport id is the one ``adb devices -l`` reported when the
        `Device` was made. Afterwards each request selects that transport
        (``host:transport-id:N``), not the serial, so a device that later takes
        the same serial never receives anything: the requests fail instead.
        Then the identity is read through that transport and kept
        (`pinned_identity`); `check_pin` compares against it. Calling it again
        keeps the first transport and identity.

        When the kept identity has a boot id, a transport that the adb server
        later renumbers is followed (see `can_repin`); without one, anything
        that makes the device be proven again fails the pin.

        Raises:
            AdbError: The adb server reported no transport id for this device,
                so it cannot be pinned (nothing is sent).
            DeviceNotFoundError: The server no longer lists that transport id
                with this serial (``details["reason"]`` is ``device_changed``);
                nothing is pinned and nothing is sent.
            AdbError: The identity could not be read, or has no build
                fingerprint, so the device could not be told apart later;
                nothing is pinned.
        """
        if self._broken is not None:
            raise self._broken_error()
        if self._transport_id is not None:
            return self._transport_id
        # The candidate stays local until it is proven; any way out but
        # success is kept as a failed pin (an interrupt included), so no
        # request of this Device goes out afterwards, not even by serial.
        succeeded = False
        try:
            # From here on a server that cannot be reached is a failure, not a
            # reason to start a new one (it would number its transports
            # anew), and no other request goes out while the first proof runs.
            self._client.stop_starting_server()
            self._repinning = True
            transport, found = self._first_proof()
            self._pinned_identity = found
            self._transport_id = transport
            self._proven = self._epoch
            succeeded = True
        except UiprobeError as error:
            self._broken = (str(error), dict(error.details))
            self._epoch += 1
            raise
        finally:
            self._repinning = False
            if not succeeded and self._broken is None:
                self._broken = (
                    f"pinning {self.serial} did not finish",
                    {"serial": self.serial, "reason": "device_changed"},
                )
                self._epoch += 1
        return self._transport_id

    @property
    def pinned_identity(self) -> DeviceIdentity | None:
        """The identity read when the transport was pinned, None before."""
        return self._pinned_identity

    @property
    def can_repin(self) -> bool:
        """Whether the device may be proven again: a renumbered transport
        followed, or the identity read again after a failed request.

        Only when the pinned identity has a boot id: without one, two
        emulators of one image cannot be told apart, so a renumbered
        transport ends the pin (``device_changed``) as it always did, and so
        does anything else that moves `pin_epoch` (a failed request): the
        next request fails the pin instead of reading the identity again.
        """
        identity = self._pinned_identity
        return identity is not None and bool(identity.boot_id)

    @property
    def repins(self) -> tuple[tuple[int, int], ...]:
        """Each move of the pin so far, as (old transport, new transport)."""
        return tuple(self._repins)

    @property
    def reconnects(self) -> int:
        """How many times the pinned device had to be proven again (a wait,
        also one that finds it back on the same number); at most
        `MAX_RECONNECTS`."""
        return self._reconnects

    @property
    def pin_epoch(self) -> int:
        """A number that moves on every event that makes earlier reads of
        the pinned device doubtful: a failed request, a wait, a move of the
        pin, a failed pin. A reader that sees the same value before and after
        its reads knows none of these happened in between."""
        return self._epoch

    @property
    def pin_watch(self) -> Callable[[], bool] | None:
        """The watch set with `set_pin_watch`, or None."""
        return self._pin_watch

    def set_pin_watch(self, watch: Callable[[], bool] | None) -> None:
        """Ask `watch` while waiting for a renumbered transport.

        When it returns True the wait ends with `DeviceNotFoundError` whose
        ``details["reason"]`` is ``pin_wait_stopped``. None removes it.
        """
        if watch is not None and not callable(watch):
            msg = "watch must be callable or None"
            raise TypeError(msg)
        self._pin_watch = watch

    @property
    def info(self) -> DeviceInfo:
        """What ``adb devices -l`` reported when the device was chosen."""
        return self._info

    def _shell(self, command: str) -> str:
        """Run `command` on the device and return its output as text."""
        return self._shell_text(command)

    def _extras(self, guard: Callable[[], None] | None) -> dict[str, object]:
        """uiprobe's own options of one shell request: the last check of a
        request that changes the device, and no server start mid-launch."""
        extras: dict[str, object] = {}
        if guard is not None:
            extras["_before_send"] = guard
        if self._launch_holds:
            extras["_no_start"] = True
        return extras

    def _shell_text(
        self,
        command: str,
        *,
        timeout: float | None = None,
        strict: bool = False,
        dispatch: _Dispatch | None = None,
    ) -> str:
        changing = command.startswith(_CHANGING_COMMANDS)
        strict = strict or changing
        raw = self._call(
            lambda pin, guard=None: self._client.shell(
                self.serial, command, timeout=timeout, **pin, **self._extras(guard)
            ),
            strict=strict,
            changing=changing,
            dispatch=dispatch,
        )
        return raw.decode("utf-8", errors="replace")
