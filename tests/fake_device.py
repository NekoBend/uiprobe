"""A scripted fake Android device that keeps state on top of `FakeAdbServer`.

Screens are described compactly (`Screen` holding `El` elements) and rendered
to uiautomator XML. `FakeAndroid` answers the commands uiprobe sends: dump,
foreground app lines, ``wm size``, ``getprop``, input, relaunch and screencap.
Taps follow the transitions written into the screens, so a crawl test can run
against a small app without any adb, emulator or network.

Formats: the XML attributes and the ``wm size`` and ``mFocusedApp`` /
``ResumedActivity`` lines follow real captures from API 24-32 and 36 emulators;
API 33-35 are modeled on API 36.
The keyboard is visible only through ``dumpsys input_method`` and its
``mInputShown=true|false`` value, which flips on every captured API (captured
from API 24-32 and 36 emulators). Its shape follows the API:
API 24-32 print one line
``mShowRequested=.. mShowExplicitlyRequested=.. mShowForced=false
mInputShown=..`` (``mShowExplicitlyRequested`` is false while shown on API 31
and 32), API 36 prints ``mInputShown`` on its own line in the
``mVisibilityStateComputer:`` block with no ``mShowRequested``. API 33-35 are
not captured: assumed to print the API 36 shape. ``mIsInputViewShown`` stays
true in every capture, so it is no signal and is printed true always. Plain
``dumpsys window`` has no ``mInputMethodWindow`` line in any capture (it
appeared only with ``dumpsys window windows`` on API 36), and the fake prints
no keyboard line there.
No capture shows the keyboard in the XML dump, so that (``keyboard_in_dump``)
and where the keyboard sits on screen are assumptions, as are the failure
output of ``am start`` and the API level (33) from which a protected screen's
screencap is a black image rather than empty output (only API 24 and 36 are
captured). ``am start -n`` of a component whose app already has screens on
the stack resumes the app's top screen: assumed, not captured.
"""

from __future__ import annotations

import re
import shlex
import struct
import threading
import zlib
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING

from tests.fake_adb_server import Reset

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from tests.fake_adb_server import FakeAdbServer, Matcher, Response

KEYBOARD_PACKAGE = "com.google.android.inputmethod.latin"
PERMISSION_PACKAGE = "com.google.android.permissioncontroller"
DUMP_OK = "UI hierchary dumped to: {}\n"
DUMP_FAILED = "ERROR: could not get idle state.\n"
# Captured on API 36 and API 30 emulators: the monkey success
# and failure outputs. Both start, after the
# wrapper's marker, with a preamble that monkey itself prints, ``  bash arg: <a>``
# per argument, then
# ``args: [a, b]``, `` arg: "<a>"`` per argument and ``data="<pkg>"`` (plus
# ``data="<category>"`` with ``-c``); the result line follows. Only API 30 and
# 36 are captured: the preamble on API 24-29 is assumed to be the same.
MONKEY_OK = (
    "Events injected: 1\n"
    "## Network stats: elapsed time=12ms (0ms mobile, 0ms wifi, 12ms not connected)\n"
)
LAUNCHER_CATEGORY = "android.intent.category.LAUNCHER"
INPUT_MARKER_SUFFIX = "; echo uiprobe-exit:$?"
MONKEY_FAILED = "** No activities found to run, monkey aborted.\n"
# Captured on API 36 and API 30 emulators: the success
# line, with the component in its short form as given. Assumed, not captured:
# the echo of a component given in full is the short form
# (``package/.Class``), as ``Intent.toShortString`` prints it.
AM_START_STARTED = "Starting: Intent {{ cmp={} }}\n"
# Assumed, not captured: the failure output of am start.
AM_START_FAILED = (
    "Starting: Intent {{ cmp={0} }}\n"
    "Error type 3\n"
    "Error: Activity class {{{1}}} does not exist.\n"
)
Bounds = tuple[int, int, int, int]


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _png(pixel: bytes) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + _chunk(b"IEND", b"")
    )


# 1x1 PNGs, built here so no binary file is needed. A FLAG_SECURE screen is a
# black PNG on API 36 (captured on an emulator) and empty output on API 24.
PNG = _png(b"\xff\xff\xff")
BLACK_PNG = _png(b"\x00\x00\x00")


@dataclass(frozen=True, slots=True)
class El:
    """One element. `go` and `long_go` name the screen a press leads to."""

    bounds: Bounds
    text: str = ""
    desc: str = ""
    rid: str = ""
    cls: str = "android.widget.TextView"
    go: str | None = None
    long_go: str | None = None
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    password: bool = False
    selected: bool = False
    hint: str = ""

    def contains(self, x: int, y: int) -> bool:
        """Return True when the point lies inside the bounds."""
        left, top, right, bottom = self.bounds
        return left <= x < right and top <= y < bottom


@dataclass(frozen=True, slots=True)
class Screen:
    """A screen of an app. Later elements are drawn on top of earlier ones."""

    name: str
    component: str  # "package/.Activity", as in the dumpsys lines
    elements: tuple[El, ...] = ()
    keyboard: bool = False  # the soft keyboard is up while this screen shows
    protected: bool = False  # screencap is blanked (FLAG_SECURE)

    @property
    def package(self) -> str:
        """The package of the activity."""
        return self.component.partition("/")[0]


def permission_dialog(
    name: str = "permission", display: tuple[int, int] = (320, 640)
) -> Screen:
    """Return a runtime-permission dialog drawn over the whole display."""
    width, height = display
    return Screen(
        name,
        f"{PERMISSION_PACKAGE}/com.android.permissioncontroller.permission.ui.GrantPermissionsActivity",
        (
            El((0, 0, width, height)),
            El(
                (20, height // 3, width - 20, height // 3 + 40),
                text="Allow Example to take pictures?",
                rid=f"{PERMISSION_PACKAGE}:id/permission_message",
            ),
            El(
                (20, height // 2, width - 20, height // 2 + 40),
                text="While using the app",
                rid=f"{PERMISSION_PACKAGE}:id/permission_allow_foreground_only_button",
                cls="android.widget.Button",
                go="back",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class Input:
    """One input command and the screen that was showing when it arrived."""

    kind: str  # tap, long_press, swipe, key, text, unknown
    args: tuple[str, ...]
    screen: str
    point: tuple[int, int] | None = None


@dataclass(slots=True)
class _Faults:
    fail_dumps: set[int] = field(default_factory=set)
    before_dump: dict[int, str] = field(default_factory=dict)
    after_dump: dict[int, str] = field(default_factory=dict)


def _hex(text: str) -> str:
    return f"{zlib.crc32(text.encode()):x}"


def _escape(value: str) -> str:
    for old, new in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        value = value.replace(old, new)
    return value.replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")


def _full_component(component: str) -> str:
    """Spell a ``package/.Class`` component with the class name in full."""
    package, _, name = component.partition("/")
    if name.startswith("."):
        name = package + name
    return f"{package}/{name}"


def _short_component(component: str) -> str:
    """Spell a component with ``.Class`` when the class is in the package."""
    package, _, name = component.partition("/")
    if name.startswith(package + "."):
        name = name[len(package) :]
    return f"{package}/{name}"


def _monkey_output(args: Sequence[str], *, ok: bool) -> str:
    """Return the monkey output for `args` (the words after ``monkey``)."""
    packages = [b for a, b in pairwise(args) if a == "-p"]
    categories = [b for a, b in pairwise(args) if a == "-c"]
    lines = [f"  bash arg: {a}" for a in args]
    lines.append(f"args: [{', '.join(args)}]")
    lines.extend(f' arg: "{a}"' for a in args)
    lines.extend(f'data="{d}"' for d in (*packages, *categories))
    return "\n".join(lines) + "\n" + (MONKEY_OK if ok else MONKEY_FAILED)


def _point(x: str, y: str) -> tuple[int, int] | None:
    """Parse two coordinates; None when either is not an integer."""
    try:
        return int(x), int(y)
    except ValueError:
        return None


def _bool(value: object) -> str:
    return "true" if value else "false"


class FakeAndroid:
    """A device with state: a stack of screens, a keyboard and recorded input.

    Strict mode is on by default: the constructor installs `allowed()` with
    `FakeAdbServer.allow_only` for this device's serial, so any other command,
    or one for another serial, or the wrong service kind, fails the test.
    It admits ``monkey`` and ``am start`` only for the packages and components
    of the device's own screens (plus ``extra_packages``) and only the LAUNCHER
    category. Pass ``strict=False`` for a test that sends other commands; that
    serves every command of this serial (`FakeAdbServer.allow_any`) even when
    another device on the same server is strict.
    Scripts added to the server before this object is created win over the
    device's own answers; use `override` for later ones.

    Attributes:
        inputs: Every input command, with the screen showing at that moment.
        stack: Names of the screens, bottom to top; the last one is showing.
        dropped: Commands whose connection was reset after they took effect.
        dropped_at: For each entry of `dropped`, how many commands the server
            had recorded when the connection was reset (the dropped one
            included), so later sends can be told from earlier ones.
        serial: The device serial.
        screens: The screens by name (read-only).
    """

    # Every pattern is matched with `search`, so each ends in \Z (a `$` would
    # also match before a trailing newline). Tokens are spelled out; there is no
    # \S, \w or \d (they admit shell metacharacters or non-ASCII characters).
    _TMP = r"/data/local/tmp/uiprobe-[0-9a-f]{16}\.xml"
    _SEGMENT = r"[A-Za-z][A-Za-z0-9_]*"
    # An extra (missing) package has a dot; a declared screen may also live in
    # a single-segment system package such as ``android``.
    _PACKAGE = rf"{_SEGMENT}(?:\.{_SEGMENT})+"
    _DECLARED_PACKAGE = rf"{_SEGMENT}(?:\.{_SEGMENT})*"
    # No ``$``: a nested class would need shell quoting. Known limit.
    _CLASS = r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    # `cat` and `screencap` go through the binary-safe ``exec:`` service
    # (`Device` reads the dump with `exec_out`); everything else is ``shell:``.
    SHELL_ALLOWED = (
        re.compile(rf"^uiautomator dump {_TMP}\Z"),
        re.compile(rf"^rm -f {_TMP}\Z"),
        re.compile(r"^wm size\Z"),
        re.compile(r"^getprop (?:ro\.build\.version\.sdk|ro\.kernel\.qemu)\Z"),
        re.compile(r"^dumpsys (?:window|activity activities|input_method)\Z"),
        re.compile(r"^dumpsys window displays\Z"),
        re.compile(r"^input tap [0-9]+ [0-9]+\Z"),
        re.compile(r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800\Z"),
        # The marker forms ``uiprobe tap`` sends; no other compound command.
        re.compile(r"^input tap [0-9]+ [0-9]+; echo uiprobe-exit:\$\?\Z"),
        re.compile(
            r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800; echo uiprobe-exit:\$\?\Z"
        ),
        # A tap that holds still for 1 to 200 ms (the default is 10).
        re.compile(
            r"^input swipe ([0-9]+) ([0-9]+) \1 \2 (?:[1-9][0-9]?|1[0-9]{2}|200)"
            r"; echo uiprobe-exit:\$\?\Z"
        ),
        re.compile(r"^input keyevent (?:KEYCODE_BACK|4)\Z"),
        # ``input text`` with the argument quoted for the device shell: always
        # in single quotes, a quote inside as '"'"'.
        re.compile(r"^input text '(?:[^']|'\"'\"')*'; echo uiprobe-exit:\$\?\Z"),
        # The marker form ``uiprobe key`` sends (``uiprobe swipe``: SWIPE_ALLOWED).
        re.compile(
            r"^input keyevent KEYCODE_[A-Z0-9_]{1,40}; echo uiprobe-exit:\$\?\Z"
        ),
    )
    # The marker form ``uiprobe swipe`` sends, admitted only by a device made
    # with ``general_swipes=True``: it also covers non-press swipes, which the
    # tap tests expect strict mode to refuse. A swipe that does not move, a
    # coordinate over 32767 and a duration outside 1..10000 are never admitted;
    # numbers have no leading zeros.
    _COORD = (
        r"(0|[1-9][0-9]{0,3}|[12][0-9]{4}|3[01][0-9]{3}"
        r"|32[0-6][0-9]{2}|327[0-5][0-9]|3276[0-7])"
    )
    SWIPE_ALLOWED = (
        re.compile(
            rf"^input swipe {_COORD} {_COORD} (?!\1 \2 ){_COORD} {_COORD} "
            r"([1-9][0-9]{0,3}|10000); echo uiprobe-exit:\$\?\Z"
        ),
    )
    EXEC_ALLOWED = (
        re.compile(rf"^cat {_TMP}\Z"),
        re.compile(r"^screencap -p\Z"),
    )

    @property
    def screens(self) -> Mapping[str, Screen]:
        """The screens by name, read-only: they are fixed at construction."""
        return MappingProxyType(self._screens)

    @classmethod
    def _check_component(cls, component: str) -> None:
        """Reject a declared component that is not ``package/class``-shaped.

        Raises:
            ValueError: The component is malformed (a declaration must not
                widen the strict allowlist with shell syntax).
        """
        if re.fullmatch(rf"{cls._DECLARED_PACKAGE}/{cls._CLASS}", component) is None:
            msg = f"not a package/class component: {component!r}"
            raise ValueError(msg)

    def allowed(
        self, extra_packages: Collection[str] = ()
    ) -> dict[str, tuple[re.Pattern[str], ...]]:
        """Return the strict-mode allowlist for this device, by service kind.

        The keys are ``shell`` and ``exec``: ``cat`` and ``screencap`` are
        admitted only as ``exec:``, every other command only as ``shell:``.

        Args:
            extra_packages: Packages the device does not have, admitted so a
                test can send the launch of a missing package (monkey, and
                am start with any component of it).

        Raises:
            ValueError: A screen's component is malformed, an extra package is
                not package-shaped, or is one the device declares (its
                components are already listed exactly).
        """
        # Judge a snapshot, validated again: the allowlist must never be wider
        # than what the constructor accepted.
        snapshot = list(self._screens.values())
        for screen in snapshot:
            self._check_component(screen.component)
        packages = sorted({s.package for s in snapshot})
        for package in extra_packages:
            if re.fullmatch(self._PACKAGE, package) is None:
                msg = f"not a package name: {package!r}"
                raise ValueError(msg)
            if package in packages:
                msg = f"the device already declares package {package!r}"
                raise ValueError(msg)
        components = sorted(
            {
                form
                for s in snapshot
                for form in (s.component, _full_component(s.component))
            }
        )
        extra = sorted(set(extra_packages))
        monkey = "|".join(re.escape(p) for p in [*packages, *extra])
        launcher = re.escape(LAUNCHER_CATEGORY)
        patterns = [
            re.compile(rf"^monkey -p (?:{monkey})(?: -c {launcher})? 1\Z"),
            re.compile(
                "^am start -n (?:" + "|".join(re.escape(c) for c in components) + r")\Z"
            ),
            # ``uiprobe launch --restart`` sends the stop in the marker form.
            re.compile(rf"^am force-stop (?:{monkey}); echo uiprobe-exit:\$\?\Z"),
        ]
        if extra:
            missing = "|".join(re.escape(p) for p in extra)
            patterns.append(re.compile(rf"^am start -n (?:{missing})/{self._CLASS}\Z"))
        return {
            "shell": (
                *self.SHELL_ALLOWED,
                *(self.SWIPE_ALLOWED if self.general_swipes else ()),
                *patterns,
            ),
            "exec": self.EXEC_ALLOWED,
        }

    def __init__(
        self,
        server: FakeAdbServer,
        serial: str,
        screens: Sequence[Screen],
        *,
        start: str,
        api: int = 36,
        display: tuple[int, int] = (320, 640),
        extra_attributes: bool | None = None,
        focused_app_line: bool = True,
        keyboard_in_dump: bool = False,
        secure_screencap: str | None = None,
        strict: bool = True,
        extra_packages: Collection[str] = (),
        general_swipes: bool = False,
    ) -> None:
        """Create the device and register it with `server`.

        Args:
            server: The adb server to answer through.
            serial: The device serial.
            screens: The screens of the apps.
            start: Name of the screen showing first.
            api: The API level whose output shapes are produced.
            display: Display size in pixels.
            extra_attributes: Whether nodes carry drawing-order and hint
                (default: API 33 and up).
            focused_app_line: Print ``mFocusedApp`` in ``dumpsys window``.
                Off, the foreground app comes from ``dumpsys activity``.
            keyboard_in_dump: assumed, not captured: add the keyboard as a
                second window to the XML dump while it is up.
            secure_screencap: ``"empty"`` (captured on API 24) or ``"black"``
                (captured on API 36); default by API level (assumed: black
                from 33, where the switch happens was not captured).
            strict: Install the strict-mode allowlist (`allowed`) on `server`
                for this serial. Off, every command of this serial is served,
                also when other devices on the server are strict.
            extra_packages: Packages the device does not have, admitted so a
                test can send the launch of a missing package (see `allowed`).
            general_swipes: Also admit the marker form of a swipe that moves
                (`SWIPE_ALLOWED`); off, only the long press form is admitted.

        Raises:
            ValueError: A component is malformed, a screen name is declared
                twice or is ``"back"`` (reserved), `start` or a ``go`` /
                ``long_go`` target is not a declared screen (``"back"`` is
                allowed for the transitions), or an extra package is invalid.
        """
        for screen in screens:
            self._check_component(screen.component)
            if screen.name == "back":
                msg = "'back' is reserved for the back transition, not a screen name"
                raise ValueError(msg)
        self._screens = {s.name: s for s in screens}
        if len(self._screens) != len(screens):
            names = [s.name for s in screens]
            dup = sorted({n for n in names if names.count(n) > 1})
            msg = f"duplicate screen names: {dup}"
            raise ValueError(msg)
        self._require(start)
        for screen in screens:
            for element in screen.elements:
                for target in (element.go, element.long_go):
                    if target is not None and target != "back":
                        self._require(target, f"screen {screen.name!r} leads to")
        self.general_swipes = general_swipes
        self.stack = [start]
        self.inputs: list[Input] = []
        self.dumps = 0
        self.api = api
        self.display = display
        # Only API 36 is captured with drawing-order and hint; 33-35 is a guess.
        self.extra_attributes = (
            api >= 33 if extra_attributes is None else extra_attributes
        )
        self.focused_app_line = focused_app_line
        self.keyboard_in_dump = keyboard_in_dump
        # Assumed: black from API 33; only API 24 (empty) and 36 (black) captured.
        self.secure_screencap = secure_screencap or ("black" if api >= 33 else "empty")
        self.serial = serial
        # False: monkey reports success but the app does not come to the front.
        self.launch_in_front = True
        # Packages stopped with ``am force-stop``, in order.
        self.stopped: list[str] = []
        # Packages whose task a stop removed while all screens were theirs.
        self._gone: set[str] = set()
        self.dropped: list[str] = []
        self.dropped_at: list[int] = []
        self.props = {"ro.build.version.sdk": str(api), "ro.kernel.qemu": "1"}
        self.faults = _Faults()
        self._files: dict[str, bytes] = {}
        self._keyboard_hidden = False
        # Lines appended to a dumpsys service's output ("window", "activity
        # activities", "input_method"), to imitate text an app prints.
        self.extra_dumpsys: dict[str, str] = {}
        # Replaces the text of ``dumpsys window displays`` when not None.
        self.displays_output: str | None = None
        self._drop_after: set[int] = set()
        # What input commands in the marker form answer (see answer_input).
        self._input_status, self._input_marker = 0, True
        self._input_output = ""
        self._server = server
        self._lock = threading.RLock()
        # Everything is validated (the allowlist included) before the server is
        # touched, so a refused construction leaves the server as it was.
        rules = self.allowed(extra_packages)
        if not any(d["serial"] == serial for d in server.devices):
            server.add_device(serial)
        server.on_shell(serial, re.compile(""), self._shell)
        server.on_exec(serial, re.compile(""), self._shell)
        if strict:
            server.allow_only(rules, serial=serial)
        else:
            server.allow_any(serial)

    # --- state ---

    @property
    def screen(self) -> Screen:
        """The screen that is showing."""
        with self._lock:
            return self.screens[self.stack[-1]]

    @property
    def keyboard_visible(self) -> bool:
        """Whether the soft keyboard is up."""
        with self._lock:
            return self.screen.keyboard and not self._keyboard_hidden

    def _require(self, name: str, what: str = "unknown screen") -> None:
        if name not in self._screens:
            msg = f"{what}: {name!r} is not a declared screen"
            raise ValueError(msg)

    def show(self, name: str) -> None:
        """Bring screen `name` to the front, as an app or dialog would.

        Raises:
            ValueError: `name` is not a declared screen.
        """
        self._require(name)
        with self._lock:
            self.stack.append(name)
            self._keyboard_hidden = False

    def fail_dump(self, *attempts: int) -> None:
        """Make the given dump attempts (1-based) fail."""
        with self._lock:
            self.faults.fail_dumps.update(attempts)

    def switch_before_dump(self, attempt: int, name: str) -> None:
        """Bring `name` to the front just before dump attempt `attempt`.

        Raises:
            ValueError: `name` is not a declared screen.
        """
        self._require(name)
        with self._lock:
            self.faults.before_dump[attempt] = name

    def switch_after_dump(self, attempt: int, name: str) -> None:
        """Bring `name` to the front right after dump attempt `attempt`.

        Raises:
            ValueError: `name` is not a declared screen.
        """
        self._require(name)
        with self._lock:
            self.faults.after_dump[attempt] = name

    def drop_after_input(self, *numbers: int) -> None:
        """Reset the connection after the given inputs (1-based) took effect."""
        with self._lock:
            self._drop_after.update(numbers)

    def answer_input(
        self, *, status: int = 0, marker: bool = True, output: str = ""
    ) -> None:
        """Choose what the device answers to input commands in the marker form.

        The command still runs (and is recorded) as usual.

        Args:
            status: The exit status printed after ``uiprobe-exit:``.
            marker: False prints no marker line, as if the shell had died.
            output: Text printed before the marker, as ``input`` does on error.
        """
        with self._lock:
            self._input_status = status
            self._input_marker = marker
            self._input_output = output

    def override(self, matcher: Matcher, response: Response) -> None:
        """Script an answer that wins over the device's own (shell and exec)."""
        self._server.on_shell(self.serial, matcher, response, first=True)
        self._server.on_exec(self.serial, matcher, response, first=True)

    # --- commands ---

    def _words(self, command: str) -> list[str]:
        """Split a command; a malformed one (an unbalanced quote) gives [].

        Only lax mode gets here with such a command. A device would answer with
        an error; the fake records an ``input`` it cannot parse as ``unknown``
        and answers nothing for any other command.
        """
        try:
            return shlex.split(command)
        except ValueError:
            if command.split()[:1] == ["input"]:
                rest = tuple(command.split()[1:])
                self.inputs.append(Input("unknown", rest, self.stack[-1]))
            return []

    def _shell(self, command: str) -> bytes | Reset:
        """Run a command; in the marker form, answer with the marker line too."""
        if not command.endswith(INPUT_MARKER_SUFFIX):
            return self._run(command)
        result = self._run(command.removesuffix(INPUT_MARKER_SUFFIX))
        if isinstance(result, Reset):
            with self._lock:
                self.dropped[-1] = command  # as the server recorded it
            return result
        with self._lock:
            out = result + self._input_output.encode()
            if self._input_marker:
                out += b"uiprobe-exit:%d\n" % self._input_status
        return out

    def _run(self, command: str) -> bytes | Reset:
        out = b""
        with self._lock:
            before = len(self.inputs)
            words = self._words(command)
            match words:
                case ["uiautomator", "dump", path]:
                    out = self._dump(path)
                case ["cat", path]:
                    out = self._files.get(path, b"")
                case ["rm", "-f", path]:
                    self._files.pop(path, None)
                case ["wm", "size"]:
                    out = b"Physical size: %dx%d\n" % self.display
                case ["getprop", name]:
                    out = self.props.get(name, "").encode() + b"\n"
                case ["dumpsys", "window", *more] if more in ([], ["displays"]):
                    text = self._dumpsys_displays() if more else self._dumpsys_window()
                    out = text.encode()
                case ["dumpsys", "activity", "activities"]:
                    out = self._dumpsys_activities().encode()
                case ["dumpsys", "input_method"]:
                    out = self._dumpsys_input_method().encode()
                case ["input", *args]:
                    self._input(args)
                case ["monkey" | "am", *_]:
                    out = self._launch_command(words)
                case ["screencap", "-p"]:
                    out = self._screencap()
            if len(self.inputs) > before and len(self.inputs) in self._drop_after:
                self.dropped.append(command)
                self.dropped_at.append(self._server.admitted)
                return Reset()
        return out

    def _launch_command(self, words: list[str]) -> bytes:
        match words:
            case ["monkey", "-p", package, *rest]:
                args = words[1:]
                categories = [c for a, c in pairwise(rest) if a == "-c"]
                # An unsupported category matches no activity, as in real monkey.
                ok = all(c == LAUNCHER_CATEGORY for c in categories)
                if ok and self.launch_in_front:
                    ok = self._launch(package)
                elif ok:
                    ok = any(s.package == package for s in self.screens.values())
                return _monkey_output(args, ok=ok).encode()
            case ["am", "force-stop", package]:
                self._stop(package)
                return b""
            case ["am", "start", "-n", component, *_]:
                full = _full_component(component)
                ok = self._launch(full.partition("/")[0], full)
                short = _short_component(full)
                started = AM_START_STARTED.format(short)
                failed = AM_START_FAILED.format(short, full)
                return (started if ok else failed).encode()
        return b""

    def _screencap(self) -> bytes:
        if not self.screen.protected:
            return PNG
        return BLACK_PNG if self.secure_screencap == "black" else b""

    def _dump(self, path: str) -> bytes:
        self.dumps += 1
        attempt = self.dumps
        if attempt in self.faults.before_dump:
            self.show(self.faults.before_dump[attempt])
        failed = attempt in self.faults.fail_dumps
        if not failed:
            self._files[path] = self.render_xml()
        if attempt in self.faults.after_dump:
            self.show(self.faults.after_dump[attempt])
        return (DUMP_FAILED if failed else DUMP_OK.format(path)).encode()

    def _input(self, args: list[str]) -> None:
        screen = self.stack[-1]
        match args:
            case ["tap", x, y] if (point := _point(x, y)) is not None:
                self.inputs.append(Input("tap", tuple(args), screen, point))
                self._press(point, long=False)
            case ["swipe", x1, y1, x2, y2, "800"] if (x1, y1) == (x2, y2) and (
                point := _point(x1, y1)
            ) is not None:
                self.inputs.append(Input("long_press", tuple(args), screen, point))
                self._press(point, long=True)
            case ["swipe", x1, y1, x2, y2, ms] if (
                (x1, y1) == (x2, y2)
                and re.fullmatch(r"[1-9][0-9]{0,2}", ms)
                and int(ms) <= 200
                and (point := _point(x1, y1)) is not None
            ):
                # A short hold in place is a tap (below the long-press timeout).
                self.inputs.append(Input("tap", tuple(args), screen, point))
                self._press(point, long=False)
            case ["swipe", *_]:
                self.inputs.append(Input("swipe", tuple(args), screen))
            case ["keyevent", *keys]:
                self.inputs.append(Input("key", tuple(keys), screen))
                if keys in (["KEYCODE_BACK"], ["4"]):
                    self._back()
            case ["text", _]:
                self.inputs.append(Input("text", tuple(args), screen))
            case _:
                # Forms the fake does not model: a source prefix, press, roll,
                # motionevent, draganddrop, a wrong argument count, and
                # coordinates that are not integers (``input tap a b``).
                self.inputs.append(Input("unknown", tuple(args), screen))

    def _press(self, point: tuple[int, int], *, long: bool) -> None:
        x, y = point
        if self.keyboard_visible and y >= self.display[1] // 2:
            return  # the keyboard window takes the press
        for element in reversed(self.screen.elements):
            if element.contains(x, y):
                if not element.enabled:
                    return  # the touch is delivered, a disabled view does nothing
                target = element.long_go if long else element.go
                if target == "back":
                    self._back()
                elif target is not None:
                    self.show(target)
                return

    def _back(self) -> None:
        if self.keyboard_visible:
            self._keyboard_hidden = True
        elif len(self.stack) > 1:
            self.stack.pop()
            self._keyboard_hidden = False

    def _stop(self, package: str) -> None:
        """Remove the screens of `package` from the stack (``am force-stop``).

        The fake has no home screen, so a stack that would become empty keeps
        its screens on show, but the task is gone: the next launch of the
        package starts it fresh from its first screen. Until then those
        screens stay in front, also with ``launch_in_front`` False, where a
        real device would show its home screen; a test of a launch that does
        not come to the front after a restart needs a screen of another app
        on the stack.
        """
        self.stopped.append(package)
        kept = [n for n in self.stack if self.screens[n].package != package]
        if kept:
            self.stack = kept
            self._keyboard_hidden = False
        else:
            self._gone.add(package)

    def _launch(self, package: str, component: str | None = None) -> bool:
        """Bring a package, or one component of it, to the front.

        When a screen of the package is on the stack, its top screen comes
        forward and the screens above it are dropped, whichever component was
        named (assumed, not captured: a real ``am start -n`` of a component of
        a running task may start a new instance or clear the task down to it).
        Otherwise the first matching screen is started. Return False when the
        device has no such screen.
        """

        def wanted(screen: Screen) -> bool:
            if screen.package != package:
                return False
            return component is None or _full_component(screen.component) == component

        if not any(wanted(screen) for screen in self.screens.values()):
            return False  # an unknown activity fails even if the app runs
        if package in self._gone:
            self._gone.discard(package)
            first = next(s.name for s in self.screens.values() if wanted(s))
            others = [n for n in self.stack if self.screens[n].package != package]
            self.stack = [*others, first]
            self._keyboard_hidden = False
            return True
        owned = [
            i
            for i, name in enumerate(self.stack)
            if self.screens[name].package == package
        ]
        if owned:
            del self.stack[owned[-1] + 1 :]
            self._keyboard_hidden = False
            return True
        first = next(s.name for s in self.screens.values() if wanted(s))
        self.show(first)
        return True

    # --- rendering ---

    def _node(
        self, index: int, order: int, el: El, package: str, children: str = ""
    ) -> str:
        left, top, right, bottom = el.bounds
        pairs = [
            ("index", str(index)),
            ("text", el.text),
            ("resource-id", el.rid),
            ("class", el.cls),
            ("package", package),
            ("content-desc", el.desc),
            ("checkable", _bool(el.checkable)),
            ("checked", _bool(el.checked)),
            ("clickable", _bool(el.go)),
            ("enabled", _bool(el.enabled)),
            ("focusable", _bool(el.focusable)),
            ("focused", _bool(el.focused)),
            ("scrollable", _bool(el.scrollable)),
            ("long-clickable", _bool(el.long_go)),
            ("password", _bool(el.password)),
            ("selected", _bool(el.selected)),
            ("bounds", f"[{left},{top}][{right},{bottom}]"),
        ]
        if self.extra_attributes:
            pairs += [("drawing-order", str(order)), ("hint", el.hint)]
        attrs = " ".join(f'{key}="{_escape(value)}"' for key, value in pairs)
        return f"<node {attrs}>{children}</node>" if children else f"<node {attrs} />"

    def _window(self, package: str, elements: Sequence[El], index: int) -> str:
        width, height = self.display
        root = El((0, 0, width, height), cls="android.widget.FrameLayout")
        nodes = "".join(
            self._node(i, i + 1, el, package) for i, el in enumerate(elements)
        )
        return self._node(index, 0, root, package, nodes)

    def render_xml(self) -> bytes:
        """Render the screen that is showing as a uiautomator dump."""
        with self._lock:
            screen = self.screen
            windows = self._window(screen.package, screen.elements, 0)
            if self.keyboard_in_dump and self.keyboard_visible:
                width, height = self.display
                keys = [
                    El(
                        (0, height // 2, width, height),
                        cls="android.inputmethodservice.KeyboardView",
                    )
                ]
                windows += self._window(KEYBOARD_PACKAGE, keys, 1)
        return (
            "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            f'<hierarchy rotation="0">{windows}</hierarchy>'
        ).encode()

    def _record(self) -> str:
        screen = self.screen
        return f"ActivityRecord{{{_hex(screen.name)} u0 {screen.component} t12}}"

    def _dumpsys_displays(self) -> str:
        """``dumpsys window displays`` as API 24 prints it; ``cur=`` is the display.

        ``displays_output`` replaces the whole text when a test sets it.
        """
        if self.displays_output is not None:
            return self.displays_output
        width, height = self.display
        return (
            "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
            "  Display: mDisplayId=0\n"
            f"    init={width}x{height} 160dpi cur={width}x{height} "
            f"app={width}x{height} rng={width}x{height}-{width}x{height}\n"
        )

    def _dumpsys_window(self) -> str:
        record = self._record()
        component = self._title_component()
        lines = [
            "WINDOW MANAGER WINDOWS (dumpsys window windows)",
            f"  mCurrentFocus=Window{{{_hex(component)} u0 {component}}}",
        ]
        if self.focused_app_line and self.api < 30:
            token = f"Token{{{_hex(record)} {record}}}"
            lines.append(f"  mFocusedApp=AppWindowToken{{{_hex(token)} token={token}}}")
            lines.append(f"    mFocusedApp={token}")
        elif self.focused_app_line:
            lines.append(f"  mFocusedApp={record}")
        return "\n".join(lines) + "\n" + self.extra_dumpsys.get("window", "")

    def _title_component(self) -> str:
        """The component with the class name in full, as window titles print it."""
        return _full_component(self.screen.component)

    def _dumpsys_input_method(self) -> str:
        shown = _bool(self.keyboard_visible)
        if self.api >= 33:
            # Captured on an API 36 emulator; API 33-35
            # are assumed to print the same.
            state = (
                "    mVisibilityStateComputer:\n"
                f"      mRequestedShowExplicitly={shown} mShowForced=false\n"
                "      mImeHiddenByDisplayPolicy=false\n"
                f"      mInputShown={shown}\n"
                "      mLastImeTargetWindow=android.os.BinderProxy@f4cef99\n"
            )
        else:
            # API 24-32; API 31 and 32 print false for the explicit flag while
            # the keyboard is shown (captured on an API 31 emulator).
            explicit = _bool(self.keyboard_visible and self.api < 31)
            state = (
                f"  mShowRequested={shown} mShowExplicitlyRequested={explicit}"
                f" mShowForced=false mInputShown={shown}\n"
            )
        # True in every capture, keyboard up or not: not a signal.
        tail = "  mIsInputViewShown=true mStatusIcon=0\n"
        return state + tail + self.extra_dumpsys.get("input_method", "")

    def _dumpsys_activities(self) -> str:
        record = self._record()
        name = "topResumedActivity=" if self.api >= 32 else "mResumedActivity: "
        return (
            f"ACTIVITY MANAGER ACTIVITIES\n  {name}{record}\n"
            f"    ResumedActivity: {record}\n"
            + self.extra_dumpsys.get("activity activities", "")
        )


# --- assertions for crawl tests ---


def _elements_at(android: FakeAndroid, event: Input) -> list[El]:
    if event.point is None:
        return []
    x, y = event.point
    return [e for e in android.screens[event.screen].elements if e.contains(x, y)]


def assert_no_press_inside(android: FakeAndroid, word: str) -> None:
    """Fail when a tap or long press landed inside an element naming `word`."""
    for event in android.inputs:
        for element in _elements_at(android, event):
            assert word.lower() not in (element.text + element.desc).lower(), (
                f"{event.kind} at {event.point} on {event.screen!r}"
                f" hit {element.text!r}"
            )


def assert_only_back_keys(android: FakeAndroid) -> None:
    """Fail when any key other than BACK was sent."""
    keys = [e for e in android.inputs if e.kind == "key"]
    assert all(e.args in {("KEYCODE_BACK",), ("4",)} for e in keys), keys


def assert_no_text_input(android: FakeAndroid) -> None:
    """Fail when ``input text`` was sent."""
    assert not [e for e in android.inputs if e.kind == "text"]


def assert_no_long_press(android: FakeAndroid) -> None:
    """Fail when a long press was sent."""
    assert not [e for e in android.inputs if e.kind == "long_press"]


def assert_input_kinds(android: FakeAndroid, allowed: Collection[str]) -> None:
    """Fail when an input of a kind outside `allowed` was sent.

    Forms the fake does not model (``press``, ``roll``, a source prefix) are
    kind ``unknown``, so they fail unless ``"unknown"`` is allowed.
    """
    other = [e for e in android.inputs if e.kind not in allowed]
    assert not other, other


def assert_inputs_only_in(android: FakeAndroid, package: str) -> None:
    """Fail when an input arrived while another app was in the foreground."""
    stray = [e for e in android.inputs if android.screens[e.screen].package != package]
    assert not stray, stray


def assert_no_resend_after_drop(android: FakeAndroid, server: FakeAdbServer) -> None:
    """Fail when a command whose connection was dropped was sent again.

    The command took effect before the connection was lost, so repeating it
    would apply the input twice. Only commands sent after the drop count: the
    same command may have been sent before it.
    """
    for command, at in zip(android.dropped, android.dropped_at, strict=True):
        later = [
            c
            for serial, _, c in server.commands[at:]
            if serial == android.serial and c == command
        ]
        assert not later, f"{command!r} was sent again after a drop"
