"""Reading which window has the focus: the `dumpsys window`, `dumpsys activity` and
`dumpsys input` parsers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from uiprobe.device import Device


# Android gives each user this many uids: uid = user * 100000 + app id.
_UIDS_PER_USER: Final = 100000


class FocusedWindow(NamedTuple):
    """The window that has input focus: its owner package, title and hash.

    ``window`` is the hash ``dumpsys window`` prints for the window, which
    tells two windows with the same owner and title apart. ``user`` is the
    Android user it belongs to (the ``u<N>`` of ``Window{<hash> u<N> ...}``),
    or -1 when the window's owner uid (``mOwnerUid=``, user * 100000 + app
    id) names another user. ``owner_uid`` is that full uid (on the emulators
    checked, API 24, 30, 33 to 36, the focused app window's equals the app's
    uid), None when not read.
    """

    owner: str
    title: str
    window: str
    user: int = 0
    owner_uid: int | None = None


_COMPONENT = r"(?P<package>[A-Za-z0-9_.$]{1,256})/(?P<cls>[A-Za-z0-9_.$]{1,256})"


_RECORD = r"ActivityRecord\{[0-9a-f]{1,16} u[0-9]{1,3} " + _COMPONENT


_TASK_SUFFIX = r"(?: t-?[0-9]{1,9})?(?: [a-z]{1,16})?"


_MAX_DUMPSYS_LINE = 4096


class _Source(NamedTuple):
    """One place in a dump that names the focused or resumed activity."""

    claims: re.Pattern[str]  # any line that claims to be this field
    shape: re.Pattern[str]  # the exact shape a genuine line has


# Window titles and some activity-manager strings are chosen by apps and may
# hold newlines, so an app can print a line that imitates any field. A forged
# line can be added but a genuine one cannot be removed, so a source counts
# only when every line that claims its field is well formed and they all name
# the same component. A window title is never a source: the activity comes
# only from the system's activity records.
_FOCUSED_APP_SOURCE: Final = _Source(
    re.compile(r"  mFocusedApp="),
    re.compile(
        r"  mFocusedApp=(?:AppWindowToken\{[0-9a-f]{1,16} "
        r"token=Token\{[0-9a-f]{1,16} )?" + _RECORD + _TASK_SUFFIX + r"\}{1,3}"
    ),
)


# `dumpsys activity activities` also prints app-chosen strings. The field is
# written ``name=`` on some versions and ``name:`` on others, and its
# indentation varies, so any indentation is accepted.
_RESUMED_SOURCE: Final = _Source(
    re.compile(r"\s*(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:]"),
    re.compile(
        r"\s*(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:] ?"
        + _RECORD
        + _TASK_SUFFIX
        + r"\}"
    ),
)


_ABSENT: Final = object()


# On a device with several displays (a foldable's cover screen), `dumpsys
# window` prints a block per display, each with its own ``mCurrentFocus`` and
# ``mFocusedApp``, in no fixed order, and one ``mTopFocusedDisplayId`` names
# the focused display. A dump is read that way when more than one line holds
# ``Display:`` then ``mDisplayId`` (any spacing), or any line that holds
# ``mTopFocusedDisplayId`` is not exactly ``  mTopFocusedDisplayId=0``;
# otherwise it is read as on a single display (an API 37 emulator prints one
# header and that exact line, API 24 a header and no such line). A forged
# header makes two, and a forged or malformed top line is not that exact
# line, so forgeries land on the stricter reading. No line counts or is
# dropped because of where a header sits: an app can forge headers in a
# window title, so a header's position proves nothing.
_DISPLAY_HEADER_FIELD: Final = re.compile(r"Display:\s*mDisplayId")


_TOP_FOCUSED_DISPLAY_FIELD: Final = "mTopFocusedDisplayId"


_TOP_FOCUSED_DISPLAY_ZERO: Final = "  mTopFocusedDisplayId=0"


_TOP_FOCUSED_DISPLAY: Final = re.compile(
    r"  mTopFocusedDisplayId=(-1|0|[1-9][0-9]{0,9})"
)


_CURRENT_FOCUS_FIELD: Final = re.compile(r"(?<![A-Za-z0-9_])mCurrentFocus\s*[=:]")


_CURRENT_FOCUS_NULL: Final = "  mCurrentFocus=null"


# The start of the line below a window block's header for a window on display 0.
_WINDOW_ON_DISPLAY_ZERO: Final = "    mDisplayId=0 "


_CURRENT_FOCUS_EXACT: Final = re.compile(
    r"  mCurrentFocus=Window\{([0-9a-f]{1,16}) u([0-9]{1,3}) ([^\r\n]{1,512})\}"
)


def _several_displays(lines: list[str]) -> bool:
    """Whether a `dumpsys window` dump is read as one with several displays.

    More than one header line, a top line other than the exact
    ``  mTopFocusedDisplayId=0`` that single-display devices print, or window
    blocks on more than one display (the exact ``    mDisplayId=N`` line right
    below a window block's header). A forged line can only add a display.
    """
    headers = sum(1 for line in lines if _DISPLAY_HEADER_FIELD.search(line))
    if headers > 1 or any(
        _TOP_FOCUSED_DISPLAY_FIELD in line and line != _TOP_FOCUSED_DISPLAY_ZERO
        for line in lines
    ):
        return True
    displays = {
        match[1]
        for index, head in enumerate(lines[:-1])
        if _WINDOW_HEAD_HASH.match(head)
        and (match := _WINDOW_DISPLAY.match(lines[index + 1])) is not None
    }
    return len(displays) > 1


def _dump_lines(text: str) -> list[str]:
    """The lines of `text`, each with at most one CR before its LF cut off.

    A CR at the very end, with no LF after it, stays.
    """
    *lines, last = text.split("\n")
    return [line.removesuffix("\r") for line in lines] + [last]


def _names_other_display(lines: list[str]) -> bool:
    """Whether the ``mTopFocusedDisplayId`` lines, all exact and agreeing,
    name a display other than 0 (missing or malformed lines do not)."""
    values: set[str] = set()
    for line in lines:
        if _TOP_FOCUSED_DISPLAY_FIELD not in line:
            continue
        match = (
            _TOP_FOCUSED_DISPLAY.fullmatch(line)
            if len(line) <= _MAX_DUMPSYS_LINE
            else None
        )
        if match is None:
            return False
        values.add(match[1])
    return len(values) == 1 and values.isdisjoint({"0", "-1"})


def _names_a_display(lines: list[str]) -> bool:
    """Whether an exact ``mTopFocusedDisplayId`` line names a display other
    than 0 (another display has the focus)."""
    return any(
        (match := _TOP_FOCUSED_DISPLAY.fullmatch(line)) is not None and match[1] != "0"
        for line in lines
        if _TOP_FOCUSED_DISPLAY_FIELD in line and len(line) <= _MAX_DUMPSYS_LINE
    )


def _display_zero_focused(lines: list[str]) -> bool:
    """Whether every ``mTopFocusedDisplayId`` line is exact and says 0.

    Every line that holds the name counts, in any spacing; there must be at
    least one.
    """
    tops = [line for line in lines if _TOP_FOCUSED_DISPLAY_FIELD in line]
    for line in tops:
        match = (
            _TOP_FOCUSED_DISPLAY.fullmatch(line)
            if len(line) <= _MAX_DUMPSYS_LINE
            else None
        )
        if match is None or match[1] != "0":
            return False
    return bool(tops)


def _read_focus_of_several(lines: list[str]) -> tuple[str, str, str] | None:
    """The focused window of a dump with several displays, or None.

    Only the shape seen on a two-display phone is accepted: every line that
    names ``mCurrentFocus`` as a field (any indentation and spacing) is a
    claim, there are as many claims as display header lines, every claim but
    one is exactly ``  mCurrentFocus=null``, and that one is an exact
    ``  mCurrentFocus=Window{...}``. Anything else is None. A title can still
    forge a header and a focus line as a pair; that pair is the remaining
    case this cannot tell (documented as best effort).
    """
    claims = [line for line in lines if _CURRENT_FOCUS_FIELD.search(line)]
    headers = sum(1 for line in lines if _DISPLAY_HEADER_FIELD.search(line))
    windows = [line for line in claims if line != _CURRENT_FOCUS_NULL]
    if len(claims) != headers or len(windows) != 1:
        return None
    line = windows[0]
    match = (
        _CURRENT_FOCUS_EXACT.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
    )
    return None if match is None else (match[1], match[3], match[2])


# In `dumpsys activity activities` of a two-display phone, ``topResumedActivity``
# and ``ResumedActivity`` were each printed once (seen on one capture only),
# while ``mFocusedApp`` was printed per display.
_RESUMED_FIELD: Final = re.compile(
    r"(?<![A-Za-z0-9_])(topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:]"
)


_RESUMED_EXACT: Final = re.compile(
    r" *(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:] ?"
    + _RECORD
    + _TASK_SUFFIX
    + r"\}"
)


def _read_resumed_of_several(text: str) -> tuple[str, str] | None:
    """The resumed activity for a dump with several displays, or None.

    Every line that names ``topResumedActivity``, ``mResumedActivity`` or
    ``ResumedActivity`` as a field (any spacing before the ``=`` or ``:``) is
    a claim and must be exact; there must be a ``topResumedActivity`` claim
    and a ``ResumedActivity`` claim, and all must name the same component.
    """
    names: set[str] = set()
    found: set[tuple[str, str]] = set()
    for line in _dump_lines(text):
        claim = _RESUMED_FIELD.search(line)
        if claim is None:
            continue
        match = (
            _RESUMED_EXACT.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
        )
        if match is None:
            return None
        names.add(claim[1])
        found.add((match["package"], match["cls"]))
    if not {"topResumedActivity", "ResumedActivity"} <= names or len(found) != 1:
        return None
    return found.pop()


def _read_source(text: str, source: _Source) -> tuple[str, str] | object | None:
    """Return the component `source` names, None if unusable, or `_ABSENT`.

    `_ABSENT` means the source says nothing: no line claims the field, or the
    only line is not a record (``null``), so nothing forged can hide behind it.
    None means the source cannot be trusted: several different lines claim
    the field, or they name different components.
    """
    lines = {
        line.rstrip("\r") for line in text.split("\n") if source.claims.match(line)
    }
    if not lines:
        return _ABSENT
    found: set[tuple[str, str]] = set()
    for line in lines:
        match = source.shape.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
        if match is None:
            return _ABSENT if len(lines) == 1 else None
        found.add((match["package"], match["cls"]))
    return found.pop() if len(found) == 1 else None


_CURRENT_FOCUS_CLAIM: Final = re.compile(r"\s*mCurrentFocus=")


# The hash and user sit at fixed places right after ``Window{``; the title is
# the rest of the record up to its final brace, so it may hold braces of its
# own (``Order {7}``) but cannot move the hash or the user.
_CURRENT_FOCUS: Final = re.compile(
    r"\s*mCurrentFocus=Window\{([0-9a-f]{1,16}) u([0-9]{1,3}) ([^\r\n]{1,512})\}\s*"
)


_WINDOW_HEAD: Final = re.compile(
    r"  Window #[0-9]{1,4} Window\{([0-9a-f]{1,16}) u([0-9]{1,3}) ([^\r\n]{1,512})\}:"
)


# Where a window block's header names its hash: any line that names a hash
# there counts as a header of that hash (a title that holds ``Window{<hash>``
# further on does not).
_WINDOW_HEAD_HASH: Final = re.compile(r"  Window #[0-9]+ Window\{([0-9a-f]+) ")


# A display's section in `dumpsys input` (API 29 and later); a forged one can
# only add a display.
_INPUT_DISPLAY: Final = re.compile(r"\s*Display: (-?[0-9]{1,10})")


# The line below a window block's header names the window's display.
_WINDOW_DISPLAY: Final = re.compile(r"    mDisplayId=(0|[1-9][0-9]{0,9}) ")


# The system writes this line; 24 has mShowToOwnerOnly, 30 and 36 showForAllUsers.
_WINDOW_OWNER: Final = re.compile(
    r"    mOwnerUid=(?P<uid>[0-9]{1,10})"
    r" (?:mShowToOwnerOnly|showForAllUsers)=(?:true|false)"
    r" package=(?P<package>[A-Za-z][A-Za-z0-9_]{0,254}"
    r"(?:\.[A-Za-z][A-Za-z0-9_]{0,254}){0,64})"
    r" appop=[A-Z_]{1,64}"
)


def displays_seen(device: Device) -> bool:
    """Whether a ``dumpsys window`` read (the full dump or ``windows``) or a
    `Device.window_at` read of `device` has shown several displays since
    `forget_displays`.

    A module function, not a method, so a subclass cannot replace it: the
    crawl asks it after the caller's last clock call and before its final
    comparison.
    """
    return device._several_seen  # noqa: SLF001 - uiprobe's own state


def forget_displays(device: Device) -> None:
    """Start a new run: no read of `device` has shown several displays yet."""
    device._several_seen = False  # noqa: SLF001 - uiprobe's own state


def _read_current_focus(text: str) -> tuple[str, str, str] | None:
    """The ``(hash, title, user)`` of the focused window, or None.

    Every line that claims ``mCurrentFocus`` must be well formed and all must
    name the same window; anything else is None. (A dump with several
    displays is read by `_read_focus_of_several` instead.)
    """
    lines = {
        line.rstrip("\r")
        for line in text.split("\n")
        if _CURRENT_FOCUS_CLAIM.match(line)
    }
    windows: set[tuple[str, str, str]] = set()
    for line in lines:
        match = (
            _CURRENT_FOCUS.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
        )
        if match is None:
            return None
        windows.add((match[1], match[3], match[2]))
    return windows.pop() if len(windows) == 1 else None


def _window_focus(
    text: str, lines: list[str], *, several: bool
) -> tuple[str, str, str] | None:
    """The ``(hash, title, user)`` of the focused window, or None.

    `several` picks the reading: a dump with several displays is read only
    while display 0 has the focus (see `_read_focus_of_several`), any other
    as before (`_read_current_focus`).
    """
    if not several:
        return _read_current_focus(text)
    return _read_focus_of_several(lines) if _display_zero_focused(lines) else None


def _read_window_owner(
    text: str, window: str, title: str, user: str, *, display_zero: bool = False
) -> FocusedWindow | None:
    """The owner of window `window` from its block, or None (see focused_window).

    With `display_zero` (a dump with several displays), the line below the
    block's header, which the system writes, must also start with
    ``mDisplayId=0``: the window is on display 0. A title with a line break
    cannot forge it, because such a title never matches the focus line. The
    lines then lose at most one CR before each LF (see `_dump_lines`).
    """
    lines = (
        _dump_lines(text)
        if display_zero
        else [line.rstrip("\r") for line in text.split("\n")]
    )
    heads = [
        index
        for index, line in enumerate(lines)
        if (named := _WINDOW_HEAD_HASH.match(line)) is not None and named[1] == window
    ]
    if len(heads) != 1 or heads[0] + 2 >= len(lines):
        return None
    index = heads[0]
    line = lines[index]
    head = _WINDOW_HEAD.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
    if head is None or head[1] != window or head[2] != user or head[3] != title:
        return None
    if display_zero and not lines[index + 1].startswith(_WINDOW_ON_DISPLAY_ZERO):
        return None
    owner = _WINDOW_OWNER.fullmatch(lines[index + 2])
    if owner is None:
        return None
    # The owner uid tells the user too (user * 100000 + app id); when it
    # disagrees with u<N>, the user is not known: -1, which no check takes.
    known = int(user) if int(owner["uid"]) // _UIDS_PER_USER == int(user) else -1
    return FocusedWindow(owner["package"], title, window, known, int(owner["uid"]))


_BOOL = r"(true|false)"


# Both genuine shapes of the line that carries ``mInputShown``: API 24-32 print
# it at the end of one flag line, API 36 on a line of its own (API 33-35 were
# not captured, so any indentation is accepted for the own-line form).
_INPUT_SHOWN_SHAPES: Final = (
    re.compile(
        rf"  mShowRequested={_BOOL} mShowExplicitlyRequested={_BOOL}"
        rf" mShowForced={_BOOL} mInputShown={_BOOL}"
    ),
    re.compile(rf"[ \t]*mInputShown={_BOOL}"),
)


def _read_input_shown(text: str) -> bool | None:
    """Return the soft keyboard state from ``dumpsys input_method`` output.

    The output holds app-chosen strings that may contain newlines, so every
    line that contains ``mInputShown=`` claims the field. The result is a bool
    only when each claiming line is a genuine shape within `_MAX_DUMPSYS_LINE`
    and all of them agree; otherwise (or with no claiming line) it is None.
    """
    values: set[str] = set()
    for raw in text.split("\n"):
        line = raw.removesuffix("\r")
        if "mInputShown=" not in line:
            continue
        if len(line) > _MAX_DUMPSYS_LINE:
            return None
        for shape in _INPUT_SHOWN_SHAPES:
            match = shape.fullmatch(line)
            if match is not None:
                values.add(match[match.lastindex or 1])
                break
        else:
            return None
    if len(values) != 1:
        return None
    return values.pop() == "true"
