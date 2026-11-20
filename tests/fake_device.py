"""A scripted fake Android device that keeps state on top of `FakeAdbServer`.

Screens are described compactly (`Screen` holding `El` elements) and rendered
to uiautomator XML. `FakeAndroid` answers the commands uiprobe sends: dump,
foreground app lines, ``wm size``, ``getprop``, input, the launcher activity
(``cmd package resolve-activity``), start and stop of an app and screencap.
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
and where the keyboard sits on screen are assumptions, as is the API level
(33) from which a protected screen's screencap is a black image rather than
empty output (only API 24 and 36 are captured). ``am start`` of a component
whose app already has screens on the stack resumes the app's top screen:
assumed, not captured. The outputs of ``cmd package resolve-activity`` and
``am start -W`` follow the captures in tests/data/launch (API 24 and 36
emulators, a Samsung API 36 phone); API 25 to 35 are assumed to print the API
24 shape of ``am start -W``.
"""

from __future__ import annotations

import re
import shlex
import struct
import threading
import zlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from tests.fake_adb_server import Reset
from uiprobe.device import IDENTITY_COMMAND

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from tests.fake_adb_server import FakeAdbServer, Matcher, Response

KEYBOARD_PACKAGE = "com.google.android.inputmethod.latin"
PERMISSION_PACKAGE = "com.google.android.permissioncontroller"
DUMP_OK = "UI hierchary dumped to: {}\n"
DUMP_FAILED = "ERROR: could not get idle state.\n"
LAUNCHER_CATEGORY = "android.intent.category.LAUNCHER"
MAIN_ACTION = "android.intent.action.MAIN"
INPUT_MARKER_SUFFIX = "; echo uiprobe-exit:$?"
# ``cmd package resolve-activity --brief -c android.intent.category.LAUNCHER
# PKG``, captured on API 24 and 36 emulators and a Samsung API 36 phone (the
# same text on all three): one line about the match, then the component in
# its short form; a package that is not installed gets RESOLVE_NONE. uiprobe
# sends it in the marker form; the emulators printed exit status 0 for both.
RESOLVE_INFO = (
    "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
)
RESOLVE_NONE = "No activity found\n"
# ``am start -W -a android.intent.action.MAIN -c
# android.intent.category.LAUNCHER -n CMP``, captured on API 24 and 36
# emulators and a Samsung API 36 phone (tests/data/launch). The component is
# echoed in its short form; assumed, not captured: also when it was given in
# full (``Intent.toShortString`` prints the short form).
AM_START_STARTING = (
    "Starting: Intent {{ act=android.intent.action.MAIN"
    " cat=[android.intent.category.LAUNCHER] cmp={} }}\n"
)
# The Warning line of a start of an app that was running: captured in front
# (API 36: delivered to the top-most instance; API 24: brought to the front).
# Assumed: an app that runs behind another one gets the brought-to-front line
# on every API.
AM_START_ON_TOP = (
    "Warning: Activity not started, intent has been delivered to currently"
    " running top-most instance.\n"
)
AM_START_TO_FRONT = (
    "Warning: Activity not started, its current task has been brought to the front\n"
)
# The rest of a success, by shape: API 36 (``LaunchState:``, COLD after a
# stop, ``UNKNOWN (0)`` while running) and API 24 (``ThisTime:``).
AM_START_OK_36 = (
    "Status: ok\nLaunchState: {0}\nActivity: {1}\nTotalTime: 0\nWaitTime: 1\nComplete\n"
)
AM_START_OK_24 = (
    "Status: ok\nActivity: {}\nThisTime: 0\nTotalTime: 0\nWaitTime: 1\nComplete\n"
)
# A class the app does not have: exit status 1 on API 36, 0 on API 24.
AM_START_FAILED = "Error type 3\nError: Activity class {{{}}} does not exist.\n"
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
    It admits ``cmd package resolve-activity`` and ``am start -W`` only in the
    form uiprobe sends, with the LAUNCHER category, and only for the packages
    and components of the device's own screens (plus ``extra_packages``).
    Pass ``strict=False`` for a test that sends other commands; that
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

    # Windows of other apps drawn over everything, top first, as (title,
    # package, touchable region); they take touches without taking the focus
    # (dumpsys input lists them above the app).
    overlays: list[tuple[str, str, tuple[int, int, int, int]]]
    # Lines appended to a dumpsys service's output, by service name.
    extra_dumpsys: dict[str, str]
    # A uid per package; packages not listed get one from their name.
    uids: dict[str, int]
    # The shared user (``android:sharedUserId``) per package, for the
    # ``sharedUser=`` line of ``dumpsys package``; packages not listed have none.
    shared_users: dict[str, str]
    # The display's orientation in ``dumpsys input`` (0 to 3); None: the input
    # reader prints no Viewport line for display 0.
    orientation: int | None = 0
    # The Android user of the focused window (``u<N>`` in its record).
    focus_user: int = 0
    # The ``rotation`` attribute of the uiautomator dump's hierarchy.
    dump_rotation: int = 0
    # The owner uid printed in the focused window's block (``mOwnerUid=``);
    # None: the owner package's uid (as on the emulators).
    focus_owner_uid: int | None = None
    # What ``cat /proc/sys/kernel/random/boot_id`` prints; None: the file
    # cannot be read (the identity command then prints an empty line).
    boot_id: str | None = "3ea6a12b-8379-445f-852a-73a2c4c3ff17"

    # Every pattern is matched with `search`, so each ends in \Z (a `$` would
    # also match before a trailing newline). Tokens are spelled out; there is no
    # \S, \w or \d (they admit shell metacharacters or non-ASCII characters).
    _TMP = r"/data/local/tmp/uiprobe-[0-9a-f]{16}\.xml"
    _SEGMENT = r"[A-Za-z][A-Za-z0-9_]*"
    # An extra (missing) package has a dot; a declared screen may also live in
    # a single-segment system package such as ``android``.
    _PACKAGE = rf"{_SEGMENT}(?:\.{_SEGMENT})+"
    _DECLARED_PACKAGE = rf"{_SEGMENT}(?:\.{_SEGMENT})*"
    # A declared class may hold ``$`` (a nested class): uiprobe sends such a
    # component in single quotes, and the allowlist lists each declared
    # spelling quoted as the shell needs it. A missing package's class
    # (`extra_packages`) is admitted only without ``$``, so unquoted.
    _CLASS = r"\.?[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
    _PLAIN_CLASS = r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    # `cat` and `screencap` go through the binary-safe ``exec:`` service
    # (`Device` reads the dump with `exec_out`); everything else is ``shell:``.
    # The window with input focus as (title, owner package) when it is not the
    # screen in front's own (the notification shade, for example). A tuple, so
    # the class default is safe to share; tests set it on an instance.
    focus_window: tuple[str, str] | None = None

    SHELL_ALLOWED = (
        re.compile(rf"^uiautomator dump {_TMP}\Z"),
        re.compile(rf"^rm -f {_TMP}\Z"),
        re.compile(r"^wm size\Z"),
        re.compile(r"^getprop (?:ro\.build\.version\.sdk|ro\.kernel\.qemu)\Z"),
        re.compile(r"^dumpsys (?:window|activity activities|input_method)\Z"),
        re.compile(r"^dumpsys window displays\Z"),
        re.compile(r"^dumpsys window windows\Z"),
        re.compile(r"^dumpsys input\Z"),
        re.compile(r"^dumpsys package [A-Za-z][A-Za-z0-9_.]*\Z"),
        re.compile(
            r"^getprop ro\.kernel\.qemu; getprop ro\.serialno;"
            r" getprop ro\.build\.fingerprint;"
            r' echo "\$\(cat /proc/sys/kernel/random/boot_id 2>/dev/null\)"\Z'
        ),
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
                test can send the launch of a missing package (the launcher
                activity read, a stop, and am start with any component of it).

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
        # Every spelling of every declared component (as declared, short and
        # full), quoted for the shell as uiprobe quotes it.
        components = sorted(
            {
                shlex.quote(form)
                for s in snapshot
                for form in (
                    s.component,
                    _full_component(s.component),
                    _short_component(s.component),
                )
            }
        )
        extra = sorted(set(extra_packages))
        names = "|".join(re.escape(p) for p in [*packages, *extra])
        start = (
            rf"^am start -W -a {re.escape(MAIN_ACTION)}"
            rf" -c {re.escape(LAUNCHER_CATEGORY)} -n"
        )
        patterns = [
            # The read, the start and the stop are sent in the marker form.
            re.compile(
                r"^cmd package resolve-activity --brief"
                rf" -c {re.escape(LAUNCHER_CATEGORY)} (?:{names})"
                r"; echo uiprobe-exit:\$\?\Z"
            ),
            re.compile(
                rf"{start} (?:"
                + "|".join(re.escape(c) for c in components)
                + r"); echo uiprobe-exit:\$\?\Z"
            ),
            re.compile(rf"^am force-stop (?:{names}); echo uiprobe-exit:\$\?\Z"),
        ]
        if extra:
            missing = "|".join(re.escape(p) for p in extra)
            patterns.append(
                re.compile(
                    rf"{start} (?:{missing})/{self._PLAIN_CLASS};"
                    r" echo uiprobe-exit:\$\?\Z"
                )
            )
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
        transport_id: int | None = None,
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
        # False: am start reports success but the app does not come to the front.
        self.launch_in_front = True
        # Packages stopped with ``am force-stop``, in order.
        self.stopped: list[str] = []
        # Packages whose task a stop removed while all screens were theirs.
        self._gone: set[str] = set()
        self.dropped: list[str] = []
        self.dropped_at: list[int] = []
        self.props = {
            "ro.build.version.sdk": str(api),
            "ro.kernel.qemu": "1",
            "ro.serialno": "EMULATOR35X1X2X0",
            "ro.build.fingerprint": f"google/sdk_gphone64/emu64xa:{api}/T/1:user/k",
        }
        self.faults = _Faults()
        self._files: dict[str, bytes] = {}
        self._keyboard_hidden = False
        # Lines appended to a dumpsys service's output ("window", "activity
        # activities", "input_method"), to imitate text an app prints.
        self.extra_dumpsys, self.overlays, self.uids, self.shared_users = (
            {},
            [],
            {},
            {},
        )
        # Replaces the text of ``dumpsys window displays`` when not None.
        self.displays_output: str | None = None
        self._drop_after: set[int] = set()
        # What input commands in the marker form answer (see answer_input), and
        # the exit status the last ``am start`` printed after its marker.
        self._input_status, self._input_marker, self._start_status = 0, True, 0
        self._input_output = ""
        self._server = server
        self._lock = threading.RLock()
        # Everything is validated (the allowlist included) before the server is
        # touched, so a refused construction leaves the server as it was.
        rules = self.allowed(extra_packages)
        if not any(d["serial"] == serial for d in server.devices):
            # ``transport_id`` is listed only when given, as old servers omit it.
            server.add_device(serial, transport_id=transport_id)
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
        inner = command.removesuffix(INPUT_MARKER_SUFFIX)
        result = self._run(inner)
        if isinstance(result, Reset):
            with self._lock:
                self.dropped[-1] = command  # as the server recorded it
            return result
        with self._lock:
            if inner.startswith("am start "):
                # am start prints its own status; answer_input does not apply.
                return result + b"uiprobe-exit:%d\n" % self._start_status
            if inner.startswith("cmd package "):
                return result + b"uiprobe-exit:0\n"  # also for no activity found
            out = result + self._input_output.encode()
            if self._input_marker:
                out += b"uiprobe-exit:%d\n" % self._input_status
        return out

    def _identity(self) -> bytes:
        """The answer to `IDENTITY_COMMAND`: three properties, the boot id."""
        names = ("ro.kernel.qemu", "ro.serialno", "ro.build.fingerprint")
        lines = [self.props.get(n, "") for n in names]
        lines.append(self.boot_id or "")
        return "".join(line + "\n" for line in lines).encode()

    def _run(self, command: str) -> bytes | Reset:
        identity = command == IDENTITY_COMMAND
        with self._lock:
            out = self._identity() if identity else b""
            before = len(self.inputs)
            words = [] if identity else self._words(command)
            match words:
                case ["uiautomator", "dump", path]:
                    out = self._dump(path)
                case ["cat", path]:
                    out = self._files.get(path, b"")
                case ["rm", "-f", path]:
                    self._files.pop(path, None)
                case ["wm", "size"]:
                    out = b"Physical size: %dx%d\n" % self.display
                case ["getprop", *names]:
                    # One name, or the identity command: ``getprop a; getprop b``.
                    wanted = [n.removesuffix(";") for n in names if n != "getprop"]
                    out = "".join(self.props.get(n, "") + "\n" for n in wanted).encode()
                case ["dumpsys", "window", *more] if more in (
                    [],
                    ["displays"],
                    ["windows"],
                ):
                    out = self._dumpsys_window_part(more).encode()
                case ["dumpsys", "activity", "activities"]:
                    out = self._dumpsys_activities().encode()
                case ["dumpsys", "input" | "input_method" | "package", *_]:
                    out = self._dumpsys_service(words[1:]).encode()
                case ["input", *args]:
                    self._input(args)
                case ["cmd", "package", "resolve-activity", *_] | ["am", *_]:
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
            case [
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                category,
                package,
            ]:
                first = next(
                    (s for s in self.screens.values() if s.package == package), None
                )
                if category != LAUNCHER_CATEGORY or first is None:
                    return RESOLVE_NONE.encode()
                return (
                    RESOLVE_INFO + _short_component(first.component) + "\n"
                ).encode()
            case ["am", "force-stop", package]:
                self._stop(package)
                return b""
            case ["am", "start", "-W", "-a", action, "-c", category, "-n", component]:
                if (action, category) == (MAIN_ACTION, LAUNCHER_CATEGORY):
                    return self._start(component).encode()
        # Any other start (lax mode only) starts nothing and fails.
        self._start_status = 1
        return b""

    def _start(self, component: str) -> str:
        """Answer ``am start -W`` of `component` and set its exit status."""
        full = _full_component(component)
        package = full.partition("/")[0]
        short = _short_component(full)
        text = AM_START_STARTING.format(short)
        known = any(_full_component(s.component) == full for s in self.screens.values())
        if not known:
            self._start_status = 1 if self.api >= 36 else 0
            return text + AM_START_FAILED.format(full)
        self._start_status = 0
        running = package not in self._gone and any(
            self.screens[n].package == package for n in self.stack
        )
        in_front = running and self.screen.package == package
        if running:
            on_top = in_front and self.api >= 36
            text += AM_START_ON_TOP if on_top else AM_START_TO_FRONT
        if self.launch_in_front:
            self._launch(package, full)
            short = _short_component(self.screen.component)
        if self.api >= 36:
            state = "UNKNOWN (0)" if running else "COLD"
            return text + AM_START_OK_36.format(state, short)
        return text + AM_START_OK_24.format(short)

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
        named (assumed, not captured: a real ``am start`` of a component of
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
            f'<hierarchy rotation="{self.dump_rotation}">{windows}</hierarchy>'
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
            f"  mCurrentFocus=Window{{{_hex(component)} u{self.focus_user}"
            f" {component}}}",
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
        if self.focus_window is not None:
            return self.focus_window[0]
        return _full_component(self.screen.component)

    def _dumpsys_window_part(self, more: list[str]) -> str:
        if more == ["displays"]:
            return self._dumpsys_displays()
        return self._dumpsys_windows() if more else self._dumpsys_window()

    def _dumpsys_windows(self) -> str:
        """``dumpsys window windows``: the focused window's block, as API 36 prints it.

        The owner line is the one the system writes (captured on emulators at
        API 24, 30 and 36); the window above it stands for the others.
        """
        title = self._title_component()
        owner = (
            self.focus_window[1]
            if self.focus_window is not None
            else self.screen.package
        )
        lines = [
            "WINDOW MANAGER WINDOWS (dumpsys window windows)",
            "  Window #0 Window{1a2b3c u0 InputMethod}:",
            "    mDisplayId=0 mSession=Session{77 1:u0a1} mClient=BinderProxy@1",
            f"    mOwnerUid=10100 showForAllUsers=false package={KEYBOARD_PACKAGE}"
            " appop=NONE",
            f"  Window #1 Window{{{_hex(title)} u{self.focus_user} {title}}}:",
            "    mDisplayId=0 taskId=5 mSession=Session{88 2:u0a2} mClient=Proxy@2",
            f"    mOwnerUid={self._focus_owner_uid(owner)} showForAllUsers=false"
            f" package={owner} appop=NONE",
        ]
        return "\n".join(lines) + "\n" + self.extra_dumpsys.get("windows", "")

    def _focus_owner_uid(self, owner: str) -> int:
        """The ``mOwnerUid=`` of the focused window's block."""
        if self.focus_owner_uid is not None:
            return self.focus_owner_uid
        return self.uid_of(owner)

    def _dumpsys_service(self, words: list[str]) -> str:
        """``dumpsys input``, ``dumpsys input_method`` or ``dumpsys package X``."""
        if words == ["input_method"]:
            return self._dumpsys_input_method()
        if words == ["input"]:
            return self._dumpsys_input()
        return self._dumpsys_package(words[1] if len(words) == 2 else "")

    def _dumpsys_package(self, name: str) -> str:
        """``dumpsys package <name>`` as the emulators print it, in short.

        The stanza of a package whose name begins with `name` comes first, and
        a ``Shared users:`` section repeats the uid after the stanzas (as on
        the emulators); neither must be read as the package's. API 24 to 33
        print ``userId=``; ``appId=`` was seen on API 34, 35 and 36 emulators,
        so the fake prints it from API 34 on.
        """
        key = "appId" if self.api >= 34 else "userId"
        other = name + "x"
        lines = [
            "Permissions:",
            "  Permission [android.permission.EXAMPLE] (1a2b3c):",
            "    uid=1000 gids=[] type=0 prot=signature",
            "Packages:",
            f"  Package [{other}] ({_hex(other)}):",
            f"    {key}={self.uid_of(other)}",
            f"    pkg=Package{{{_hex(other)} {other}}}",
            f"  Package [{name}] ({_hex(name)}):",
            f"    {key}={self.uid_of(name)}",
        ]
        shared = self.shared_users.get(name)
        if shared is not None:
            lines.append(
                f"    sharedUser=SharedUserSetting{{{_hex(shared)}"
                f" {shared}/{self.uid_of(name)}}}"
            )
        lines += [
            f"    pkg=Package{{{_hex(name)} {name}}}",
            "    User 0: ceDataInode=1 installed=true hidden=false",
            "      gids=[3003]",
        ]
        lines += ["", "Queries:", "  system apps queryable: false"]
        if shared is not None:
            lines += [
                "Shared users:",
                f"  SharedUser [{shared}] ({_hex(shared)}):",
                f"    {key}={self.uid_of(name)}",
            ]
        lines.append("Dexopt state:")
        return "\n".join(lines) + "\n" + self.extra_dumpsys.get("package", "")

    def uid_of(self, package: str) -> int:
        """The uid the fake system gave `package` (stable, apps from 10000)."""
        if package in self.uids:
            return self.uids[package]
        return 10000 + zlib.crc32(package.encode()) % 9000

    def _input_line(
        self,
        index: int,
        title: str,
        region: tuple[int, int, int, int] | None,
        uid: int,
        *,
        touchable: bool = True,
    ) -> str:
        """One window line of ``dumpsys input`` in the form of `self.api`."""
        hash_ = _hex(title)
        area = "<empty>" if region is None else "[{},{}][{},{}]".format(*region)
        frame = "[0,0][{},{}]".format(*self.display)
        if self.api >= 33:  # API 36 form (33 to 35 not captured)
            config = "0x0" if touchable else "NOT_FOCUSABLE | NOT_TOUCHABLE"
            return (
                f"      {index}: name={hash_} {title}, id={index + 50}, displayId=0,"
                f" inputConfig={config}, alpha=1, frame={frame}, globalScale=1,"
                f" applicationInfo.name=, applicationInfo.token=<null>,"
                f" touchableRegion={area}, ownerPid={index + 600}, ownerUid={uid},"
                " dispatchingTimeout=5000ms"
            )
        if self.api >= 31:
            # NOT_TOUCH_MODAL: a touch outside the region is not this window's.
            flags = (
                "NOT_TOUCH_MODAL | SPLIT_TOUCH"
                if touchable
                else "NOT_FOCUSABLE | NOT_TOUCHABLE"
            )
            return (
                f"    {index}: name='{hash_} {title}', id={index + 50},"
                " displayId=0, portalToDisplayId=-1, paused=false, focusable=true,"
                " hasWallpaper=false,"
                f" visible=true, alpha=1.00, flags={flags}, type=1, frame={frame},"
                " globalScale=1.000000, applicationInfo.name=,"
                f" applicationInfo.token=<null>, touchableRegion={area},"
                f" inputFeatures=0x0, ownerPid={index + 600}, ownerUid={uid},"
                " dispatchingTimeout=5000ms"
            )
        flags = "0x81810120" if touchable else "0x81810130"
        return (
            f"    {index}: name='Window{{{hash_} u0 {title}}}', displayId=0,"
            " paused=false, hasFocus=false, hasWallpaper=false, visible=true,"
            f" canReceiveKeys=false, flags={flags}, type=0x00000001, layer=21010,"
            f" frame={frame}, scale=1.000000, touchableRegion={area},"
            f" inputFeatures=0x00000000, ownerPid={index + 600}, ownerUid={uid},"
            " dispatchingTimeout=5000.000ms"
        )

    def _dumpsys_input(self) -> str:
        """``dumpsys input``: the dispatcher's window list, top first."""
        width, height = self.display
        rows: list[tuple[str, tuple[int, int, int, int] | None, int, bool]] = [
            ("StatusBar", (0, 0, width, 24), self.uid_of("com.android.systemui"), True)
        ]
        rows += [(t, r, self.uid_of(p), True) for t, p, r in self.overlays]
        if self.keyboard_visible:
            region = (0, height // 2, width, height)
            rows.append(("InputMethod", region, self.uid_of(KEYBOARD_PACKAGE), True))
        owner = (
            self.focus_window[1]
            if self.focus_window is not None
            else self.screen.package
        )
        rows.append(
            (self._title_component(), (0, 0, width, height), self.uid_of(owner), True)
        )
        rows.append(
            ("ImageWallpaper", None, self.uid_of("com.android.systemui"), False)
        )
        indent = "    " if self.api >= 33 else "  "
        lines = ["INPUT MANAGER (dumpsys input)", "Input Reader State:"]
        if self.orientation is not None:
            lines.append(self._viewport(self.orientation))
        lines += ["Input Dispatcher State:", "  DispatchEnabled: true"]
        if self.api >= 33:
            lines += [
                "  Display: 0",
                "    logicalSize={}x{}".format(*self.display),
                "        transform (ROT_0) (IDENTITY)",
            ]
        lines.append(f"{indent}Windows:")
        for index, (title, region, uid, touchable) in enumerate(rows):
            lines.append(
                self._input_line(index, title, region, uid, touchable=touchable)
            )
            if self.api >= 31:
                lines.append("      transform (ROT_0) (IDENTITY)")
        lines.append("  Connections:")
        return "\n".join(lines) + "\n" + self.extra_dumpsys.get("input", "")

    def _viewport(self, orientation: int) -> str:
        """The input reader's Viewport line of display 0 (API 24 and API 36
        forms as seen on emulators; API 26 to 32 were not checked)."""
        width, height = self.display
        if orientation % 2:
            width, height = height, width
        frame = f"[0, 0, {width}, {height}]"
        if self.api >= 29:
            return (
                "      Viewport INTERNAL: displayId=0, uniqueId=local:0, port=0,"
                f" orientation={orientation}, logicalFrame={frame},"
                f" physicalFrame={frame}, deviceSize=[{width}, {height}],"
                " isActive=[1]"
            )
        return (
            f"      Viewport: displayId=0, orientation={orientation},"
            f" logicalFrame={frame}, physicalFrame={frame},"
            f" deviceSize=[{width}, {height}]"
        )

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
