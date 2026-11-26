"""`_InputMixin`: taps, swipes, scrolls, keys and text."""

from __future__ import annotations

import re

from uiprobe.adb import AdbError, quote_args
from uiprobe.device.failure import _EXIT_SUFFIX, _input_failure, _lines
from uiprobe.device.gestures import (
    _SWIPE_SLACK,
    DEFAULT_PRESS_MS,
    DEFAULT_SWIPE_MS,
    LONG_PRESS_MS,
    MAX_WHEEL_AMOUNT,
    DisplaySizeUnknownError,
    _check_direction,
    _check_duration,
    _check_press_ms,
    _press_point,
    _scroll_area,
    _scroll_points,
)
from uiprobe.device.keys import RefusedKeys, _key_code
from uiprobe.device.parse import _parse_display_size
from uiprobe.device.select import PIN_NOT_PROVEN, _dispatch
from uiprobe.device.state import _DeviceState
from uiprobe.device.text import (
    _piece_failure,
    _single_quoted,
    _text_pieces,
    _typing_stopped,
)
from uiprobe.errors import InputFailedError, UiprobeError
from uiprobe.model import Bounds, Element

_SCROLL_LINE = re.compile(
    r"(?:(?P<source>[A-Za-z_]+)\s+)?scroll\s+<x>\s+<y>(?P<rest>.*)"
)
_AXIS_LIST = re.compile(r"\[\s*--axis\s+(?P<axes>[^\]]*)\]")
_DEFAULT_SOURCES = re.compile(r"\(\s*Default:\s*(?P<sources>[^)]*)\)")
_PLACEHOLDER = re.compile(r"<[^<>\s]+>")


def _line_sources(found: re.Match[str]) -> set[str]:
    """The sources a ``scroll <x> <y>`` line is for: the word before the
    command, else the ``(Default: ...)`` of the line; none when it names none."""
    if found["source"]:
        return {found["source"].lower()}
    default = _DEFAULT_SOURCES.search(found["rest"])
    if default is None:
        return set()
    return {word.lower() for word in re.split(r"[\s,]+", default["sources"]) if word}


def _lists_wheel(usage: str) -> bool:
    """Whether the usage text of ``input`` lists the ``mouse`` source and a
    ``scroll <x> <y>`` command of that source with ``VSCROLL`` as an axis.

    Read per source: the scroll line must name the ``mouse`` source (as the
    word before the command, or in its ``(Default: ...)``); a line for another
    source (``rotary``, say) that lists ``VSCROLL`` is no support, and neither is
    a line that names no source. The axis is read from the bracketed
    ``[--axis ...]`` option list of that line only, split into comma and space
    separated words: ``VSCROLL`` must be a word of its own, followed by its value
    placeholder (``<v>``) or by the end of the list. The word anywhere else
    (outside the brackets, in a note such as ``VSCROLL unsupported``, inside a
    longer word) does not count.
    """
    listed = [line.strip() for line in _lines(usage)]
    mouse = any(
        line == "mouse"
        or (line.startswith("The sources are") and "mouse" in re.split(r"[\s,]+", line))
        for line in listed
    )
    for line in listed:
        found = _SCROLL_LINE.fullmatch(line)
        if found is None or "mouse" not in _line_sources(found):
            continue
        axes = _AXIS_LIST.search(found["rest"])
        if axes is None:
            continue
        words = [w for w in re.split(r"[,\s]+", axes["axes"]) if w]
        for index, word in enumerate(words):
            after = words[index + 1] if index + 1 < len(words) else None
            if word == "VSCROLL" and (after is None or _PLACEHOLDER.fullmatch(after)):
                return mouse
    return False


class _InputMixin(_DeviceState):
    """Sending taps, swipes, scrolls, keys and text."""

    def tap(
        self,
        target: Element | tuple[int, int] | list[int],
        *,
        long: bool = False,
        press_ms: int = DEFAULT_PRESS_MS,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> tuple[int, int]:
        """Press one point of the screen showing now; return the point pressed.

        `target` is an `Element` or a point ``(x, y)`` (a tuple or list of two
        ints, each 0 to 32767). An element is accepted only when it is one of
        the elements of the `Screen` returned by the latest `dump` of this
        device (the same object, not an equal copy); the point is then
        ``screen.tap_point(element)``, the center of the part of the element
        that is on the display, the same point ``uiprobe tap`` presses and
        ``uiprobe find`` prints as ``center``. An element that has no visible
        part (under 8 x 8 pixels counts as none) is not pressed, and neither is
        one that is covered: the topmost meaningful element at the point must
        be the element or a descendant of it (see `Screen.tap_point`). A point
        ``(x, y)`` given directly is pressed as given, with no check, because
        the caller named the point.

        By default a tap holds still for `press_ms` = 10 ms and runs ``input
        swipe X Y X Y 10`` (the point does not move). A plain ``input tap`` is
        barely visible in Android's "Pointer location" developer option; a 10 ms
        hold is (measured on one device, a Galaxy S25 Ultra at 60 Hz: 10 ms
        visible, 6 ms not). `press_ms` is an int from 0 to 200 (a bool is not
        accepted); 0 runs the plain ``input tap X Y``. The upper bound stays below
        Android's usual default long-press time, about 400 to 500 ms (longer
        with accessibility settings); an app may use its own shorter threshold,
        and a longer hold could turn into a long press. With `long` a press of
        800 ms runs ``input swipe X Y X Y 800``; it cannot be combined with a
        `press_ms` other than the default (``ValueError``). Every form is
        followed by ``echo uiprobe-exit:$?`` to read the device-side exit
        status. The command is sent exactly once, in one shell call. It is
        never retried or resent, not even after a connection error (that error
        propagates unchanged), because a resend could press twice.

        ``input`` prints usage text or an exception when it fails, and its exit
        status is not always nonzero then, so success needs the marker line
        ``uiprobe-exit:0`` as the last line and no other line that is not
        empty (empty lines are ignored). `InputFailedError` does not prove
        that nothing was pressed (``input`` may have acted before it failed),
        so callers must not blindly repeat the tap. The app in front is not
        checked.

        Raises:
            TypeError: `target`, `long` or `press_ms` has the wrong type, or a
                coordinate is not an int (a bool is not accepted).
            ValueError: A coordinate is outside 0 to 32767, `press_ms` is
                outside 0 to 200, `long` is combined with a `press_ms` other
                than the default, or `target` is an
                `Element` that is not one of the elements of the latest
                `dump` of this device (none yet, an older dump, another
                device, or built by hand). Dump again, or pass
                ``screen.tap_point(element)`` as a point.
            ElementNotVisibleError: The element has no visible part on the
                display of the latest dump, is under 8 x 8 pixels there, or is
                covered by another meaningful element at its press point
                (``details["reason"]`` says which); nothing is sent.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"tap"`` or ``"long_tap"``), ``point``,
                ``reason`` (``no_exit_status``, ``exit_status`` or
                ``unexpected_output``), ``exit_status`` for the second, and
                ``output`` when there is other text.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
        if not isinstance(long, bool):
            msg = f"long must be a bool, not {type(long).__name__}"
            raise TypeError(msg)
        hold = _check_press_ms(press_ms)
        if long and hold != DEFAULT_PRESS_MS:
            msg = "press_ms cannot be combined with long=True (a long press is 800 ms)"
            raise ValueError(msg)
        if isinstance(target, Element):
            latest = self._latest_screen
            if latest is None or not any(e is target for e in latest.elements):
                msg = (
                    "this element is not from the latest dump of this device; "
                    "dump again and use an element of that screen, or pass "
                    "screen.tap_point(element) as a point"
                )
                raise ValueError(msg)
            x, y = _press_point(latest.tap_point(target))
        else:
            x, y = _press_point(target)
        point = (x, y)
        if long or hold > 0:
            words = ("input", "swipe", str(x), str(y), str(x), str(y))
            words = (*words, str(LONG_PRESS_MS if long else hold))
        else:
            words = ("input", "tap", str(x), str(y))
        output = self._shell_text(quote_args(*words) + _EXIT_SUFFIX, dispatch=dispatch)
        failure = _input_failure(
            "long_tap" if long else "tap", {"point": list(point)}, output
        )
        if failure is not None:
            raise self._doubt(failure)
        return point

    def _wheel_scroll(
        self,
        point: tuple[int, int],
        amount: int,
        *,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> tuple[int, int]:
        """Turn a mouse wheel one way (content further down) at one point.

        For uiprobe's crawl only, not part of the API; `scroll` and `swipe` are
        unchanged. It sends ``input mouse scroll X Y --axis VSCROLL,-N`` once,
        with `_send_by` and `_epoch` checked on the request's own connection
        right before it is written, exactly as `tap` does. Unlike a touch, the
        wheel event goes to the window under the pointer, not to the focused
        window: whoever calls this must have checked which window that is.
        `amount` is N, from 1 to `MAX_WHEEL_AMOUNT`; the sign is always
        negative, so this never scrolls up. Returns the point.

        Raises:
            TypeError: `amount` is not an int, or a private argument has the
                wrong type.
            ValueError: `amount` is outside 1 to `MAX_WHEEL_AMOUNT`, or a
                coordinate is not usable (see `tap`).
            InputFailedError: The device did not report success.
        """
        dispatch = _dispatch(_send_by, _epoch)
        if type(amount) is not int:
            msg = f"amount must be an int, not {type(amount).__name__}"
            raise TypeError(msg)
        if not 1 <= amount <= MAX_WHEEL_AMOUNT:
            msg = f"amount must be from 1 to {MAX_WHEEL_AMOUNT}, not {amount}"
            raise ValueError(msg)
        x, y = _press_point(point)
        words = ("input", "mouse", "scroll", str(x), str(y), "--axis")
        words = (*words, f"VSCROLL,-{amount}")
        output = self._shell_text(quote_args(*words) + _EXIT_SUFFIX, dispatch=dispatch)
        failure = _input_failure("wheel", {"point": [x, y], "amount": amount}, output)
        if failure is not None:
            raise self._doubt(failure)
        return (x, y)

    def _wheel_supported(self) -> bool:
        """Whether the usage text of ``input`` lists the wheel scroll (see
        `_lists_wheel`).

        Read through the ordinary shell path, nothing is changed on the device.
        When the read fails (any adb error but a lost device) or the usage does
        not list it, the answer is False: the crawl then does not scroll.
        """
        try:
            text = self._shell_text("input")
        except (AdbError, InputFailedError):
            return False
        return _lists_wheel(text)

    def swipe(
        self,
        start: tuple[int, int] | list[int],
        end: tuple[int, int] | list[int],
        *,
        duration_ms: int = DEFAULT_SWIPE_MS,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Drag one finger from `start` to `end`; return both points.

        `start` and `end` are points ``(x, y)`` (a tuple or list of two ints,
        each 0 to 32767; a bool is not an int). `duration_ms` is an int from 1
        to 10000. A swipe that does not move (`start` equals `end`) is a press
        and is refused; use `tap`.

        The command is ``input swipe X1 Y1 X2 Y2 MS`` followed by ``echo
        uiprobe-exit:$?`` (see `tap` for how the result is read). It is sent
        exactly once, in one shell call whose read timeout is `duration_ms`
        / 1000 + 10 seconds, and it is never retried or resent, not even after
        a connection error (that error propagates unchanged), because a
        resend could swipe twice. `InputFailedError` does not prove that
        nothing moved, so callers must not blindly repeat the swipe. The app in
        front is not checked, and a swipe from a screen edge can trigger a
        system gesture.

        Raises:
            TypeError: A point or `duration_ms` has the wrong type, or a
                coordinate is not an int (a bool is not accepted).
            ValueError: A coordinate is outside 0 to 32767, `duration_ms` is
                outside 1 to 10000, or `start` equals `end`.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"swipe"``), ``from``, ``to``,
                ``duration_ms``, ``reason`` (``no_exit_status``,
                ``exit_status`` or ``unexpected_output``), ``exit_status`` for
                the second, and ``output`` when there is other text.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        """
        first = _press_point(start)
        last = _press_point(end)
        duration = _check_duration(duration_ms)
        if first == last:
            msg = "a swipe that does not move is a press; use tap"
            raise ValueError(msg)
        words = ("input", "swipe", *map(str, (*first, *last, duration)))
        output = self._shell_text(
            quote_args(*words) + _EXIT_SUFFIX,
            timeout=duration / 1000 + _SWIPE_SLACK,
        )
        where: dict[str, object] = {
            "from": list(first),
            "to": list(last),
            "duration_ms": duration,
        }
        failure = _input_failure("swipe", where, output)
        if failure is not None:
            raise self._doubt(failure)
        return first, last

    def scroll(
        self,
        direction: str,
        *,
        within: Element | None = None,
        duration_ms: int = DEFAULT_SWIPE_MS,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Swipe along the middle of an area of the screen; return the points.

        `direction` is the way the finger moves: ``"up"`` (which shows content
        further down), ``"down"``, ``"left"`` or ``"right"``. The area comes
        from the screen of the latest `dump` of this device: the visible part
        of `within` (an element of that screen, the same object, as for
        `tap`), or the whole display without it. The area is shrunk by 20% of
        its width on the left and on the right and by 20% of its height at
        the top and at the bottom, then clamped to stay 5% of the display
        width (height) away from the left and right (top and bottom) display
        edges, so the gesture avoids the system gestures at the edges. Every
        share is rounded down. The rectangle is half-open (its right and
        bottom edges are exclusive, as for element bounds), so the swipe runs
        from the first to the last pixel of it along the middle (center
        rounded down) across the movement, and both ends are inside it. With
        the rectangle ``left, top, right, bottom``: ``"up"`` goes from
        ``(center_x, bottom - 1)`` to ``(center_x, top)``, ``"down"`` the
        reverse, ``"left"`` from ``(right - 1, center_y)`` to ``(left,
        center_y)``, ``"right"`` the reverse. A rectangle under 2 pixels wide
        or high is refused. Then `swipe` sends it, once, with its rules.

        Raises:
            TypeError: `direction` is not a str, `within` is not an `Element`
                or None, or `duration_ms` has the wrong type.
            ValueError: `direction` is not one of the four, there is no dump
                yet ("dump the screen first"), the display size of the latest
                dump is unknown (`wm size` could not be read; use `swipe`
                with explicit points), `within` is not an element of the
                latest dump of this device, the area is too small to scroll,
                or the points or `duration_ms` are not usable (see `swipe`).
            ElementNotVisibleError: `within` has no visible part (a part
                under 8 x 8 pixels counts as none). It is not checked for a
                cover.
            InputFailedError: As for `swipe`, and the device errors listed
                there (`AdbError`, `AdbServerUnavailableError`,
                `DeviceNotFoundError`, `DeviceOfflineError`,
                `DeviceUnauthorizedError`).

        All but the device errors are raised before anything is sent to the
        adb server.
        """
        _check_direction(direction)
        if within is not None and not isinstance(within, Element):
            msg = f"within must be an Element or None, not {type(within).__name__}"
            raise TypeError(msg)
        latest = self._latest_screen
        if latest is None:
            msg = "dump the screen first"
            raise ValueError(msg)
        if not self._latest_display_measured:
            msg = (
                "the display size is unknown (wm size could not be read), so "
                "the scroll area cannot be placed; use swipe with explicit points"
            )
            raise ValueError(msg)
        area = _scroll_area(latest, within)
        start, end = _scroll_points(direction, area, latest.display)
        return self.swipe(start, end, duration_ms=duration_ms)

    def scroll_display(
        self, direction: str, *, duration_ms: int = DEFAULT_SWIPE_MS
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Swipe along the middle of the whole display; return the points.

        The same gesture as `scroll` without `within`, but it needs no `dump`:
        it reads the current display size from ``dumpsys window displays``
        (one request: the ``cur=`` size of display 0, already rotated) and
        sends the swipe, so it makes two adb requests where a dump and a
        scroll make many more. `direction` and the geometry are those of
        `scroll`, with the whole display as the area. It does not change the
        latest dump of this device.

        Raises:
            TypeError: `direction` is not a str or `duration_ms` has the
                wrong type.
            ValueError: `direction` is not one of the four, the display size
                cannot be read (a `DisplaySizeUnknownError`; use `dump` and
                `scroll`, or `swipe` with explicit points), the display is
                too small to scroll, or `duration_ms` is not usable (see
                `swipe`).
            InputFailedError: As for `swipe`, and the device errors listed
                there (`AdbError`, `AdbServerUnavailableError`,
                `DeviceNotFoundError`, `DeviceOfflineError`,
                `DeviceUnauthorizedError`).

        All but the device errors are raised before any input is sent.
        """
        _check_direction(direction)
        _check_duration(duration_ms)
        size = _parse_display_size(self._shell("dumpsys window displays"))
        if size is None:
            msg = (
                "the display size is unknown (dumpsys window displays gave no "
                "usable size); dump and scroll, or use swipe with explicit points"
            )
            raise DisplaySizeUnknownError(msg)
        start, end = _scroll_points(direction, Bounds(0, 0, size[0], size[1]), size)
        return self.swipe(start, end, duration_ms=duration_ms)

    def key(
        self,
        name: str,
        *,
        refused: RefusedKeys | None = None,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> str:
        """Press one key; return the key code name that was sent.

        `name` is an alias from `KEY_ALIASES` (``back``, ``home``, ...;
        case-insensitive) or a full key code name matching
        ``KEYCODE_[A-Z0-9_]{1,40}``, which is sent as given (it is not
        upper-cased, so ``keycode_back`` is refused). A bare number is refused
        because ``input keyevent`` can read it as a digit and type it. A
        ``KEYCODE_`` name made only of two or more digits after ``KEYCODE_``
        (for example ``KEYCODE_26``) is refused too, because Android may read
        it as a number; ``KEYCODE_0`` to ``KEYCODE_9`` are the digit keys and
        are accepted. A key that acts on the whole device and not on the app
        in front (power, calls, sound, brightness, screenshots, assistants,
        keys that open other apps; see `DEVICE_WIDE_KEYS` and
        `DEVICE_WIDE_KEY_PREFIXES`) is refused. `refused` None uses those
        built-in lists; a `RefusedKeys` replaces them (for example with the
        lists of the settings file), except that the keys of
        `FIXED_DEVICE_KEYS` are refused whatever it holds.

        The command is ``input keyevent KEYCODE_...`` followed by ``echo
        uiprobe-exit:$?`` (see `tap` for how the result is read). It is sent
        exactly once, never retried or resent, not even after a connection
        error (that error propagates unchanged). `InputFailedError` does not
        prove that no key was pressed, so callers must not blindly repeat it.
        The app in front is not checked.

        Raises:
            TypeError: `name` is not a str, or `refused` is neither None nor
                a `RefusedKeys`.
            ValueError: `name` is empty, a number, a ``KEYCODE_`` name of
                two or more digits (``KEYCODE_26``), a key that acts on the
                whole device (`DEVICE_WIDE_KEYS`, `DEVICE_WIDE_KEY_PREFIXES`,
                or `refused`),
                or neither an alias nor a ``KEYCODE_`` name.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"key"``), ``key``, ``reason`` and, as for
                `tap`, ``exit_status`` and ``output``.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
        if refused is not None and not isinstance(refused, RefusedKeys):
            kind = type(refused).__name__
            msg = f"refused must be a RefusedKeys or None, not {kind}"
            raise TypeError(msg)
        code = _key_code(name, refused)
        output = self._shell_text(
            quote_args("input", "keyevent", code) + _EXIT_SUFFIX, dispatch=dispatch
        )
        failure = _input_failure("key", {"key": code}, output)
        if failure is not None:
            raise self._doubt(failure)
        return code

    def type_text(self, text: str) -> None:
        """Type `text` into the field that has focus, as printable ASCII.

        Only the characters 0x20 to 0x7E are typed. Anything else is refused
        before anything is sent: a character outside ASCII (it failed on every
        Android version tried: emulators at API 24, 30 and 36), a newline or
        tab (they can act as Enter or move the focus; press Enter with `key`),
        any other control character, and empty text. A str subclass is read
        once and used as a plain str with the same characters. The text goes
        to whatever field has focus in whatever app is in front; nothing is
        checked afterwards.

        The text is sent as ``input text`` with the argument quoted for the
        device shell (always in single quotes, a quote inside as ``'"'"'``),
        in pieces of at most `TYPE_PIECE_SIZE` characters; the text is split
        before it is quoted. ``input text`` turns the two characters ``%s``
        into a space, so a piece never contains ``%`` directly followed by
        ``s``: the text is split between them. Each piece is sent once, in
        order, as exactly ``input text <quoted piece>; echo uiprobe-exit:$?``,
        and must report exit status 0 and print nothing else, or typing stops.
        Exit status 0 does not prove that the text arrived.

        Raises:
            TypeError: `text` is not a str.
            ValueError: `text` is empty or has a character that is not typed.
                The error is a `TextNotTypedError`; its ``details`` hold
                ``character`` (``U+XXXX``), ``escaped`` and ``position``.
            InputFailedError: A piece did not report success; ``details``
                hold ``reason``, ``piece``, ``pieces`` and ``pieces_done``
                (no exit status: the device prints it, so it could repeat
                the text).
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        An error while sending is a new error of the same class whose message
        and ``details`` hold only fixed words and numbers (``piece``,
        ``pieces``, ``pieces_done``): never the text, and nothing the device or
        adb answered, which can repeat it. It does not chain to the error uiprobe
        caught (a caller's own exception being handled stays its context).
        Typing that stops can leave the earlier pieces typed, and the piece it
        stopped at typed in part or in full.
        """
        pieces = _text_pieces(text)  # from one plain str, checked
        for number, piece in enumerate(pieces, start=1):
            failure = self._type_piece(piece, number, len(pieces))
            if failure is not None:
                # outside any handler: no chained error to read
                raise self._doubt(failure)

    def _type_piece(self, piece: str, number: int, pieces: int) -> UiprobeError | None:
        """Send one piece; return the error to raise, or None when it worked.

        A failed request leaves only its error class here, so the error that
        `type_text` raises chains to nothing that holds the text.
        """
        command = f"input text {_single_quoted(piece)}{_EXIT_SUFFIX}"
        kind: type[UiprobeError] | None = None
        unsent = False
        output = ""
        try:
            output = self._shell_text(command)
        except UiprobeError as exc:
            kind = type(exc)
            unsent = exc.details.get("reason") == PIN_NOT_PROVEN
        if kind is not None:
            return _typing_stopped(kind, number, pieces, unsent=unsent)
        return _piece_failure(output, number, pieces)
