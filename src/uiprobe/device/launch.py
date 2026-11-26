"""Starting an app: the checks and the reading of what `am start` printed."""

from __future__ import annotations

import re
from typing import Final, NamedTuple

from uiprobe.crawl_preview import MAX_PACKAGE_LENGTH, PACKAGE_PATTERN
from uiprobe.device.failure import _EXIT_VALUE, EXIT_MARKER, _escaped, _lines

MAX_LAUNCH_TIMEOUT: Final = 600.0


# The lines ``resolve-activity --brief`` prints before the component (captured
# on API 24 and 36 emulators and a Samsung API 36 phone:
# ``priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true``).
_RESOLVE_INFO = re.compile(
    r"priority=-?[0-9]{1,10} preferredOrder=-?[0-9]{1,10}"
    r" match=0x[0-9a-f]{1,8} specificIndex=-?[0-9]{1,10} isDefault=(?:true|false)"
)


# The class part of a component: dot-separated names of ASCII letters, digits,
# ``_`` and ``$``, optionally starting with ``.`` (short for the package).
_CLASS_NAME = re.compile(r"\.?[A-Za-z0-9_$]+(?:\.[A-Za-z0-9_$]+)*")


_MAX_CLASS_NAME: Final = 255


# The lines ``am start -W`` prints, as captured on API 24 and 36 emulators and
# a Samsung API 36 phone. The first line echoes the component (short form);
# a Warning line is one of the two captured ones (the app was running); the
# lines with times and the launch state say nothing about success.
_START_ECHO: Final = (
    "Starting: Intent {{ act=android.intent.action.MAIN"
    " cat=[android.intent.category.LAUNCHER] cmp={} }}"
)


_START_WARNINGS: Final = frozenset(
    {
        "Warning: Activity not started, intent has been delivered to currently"
        " running top-most instance.",
        "Warning: Activity not started, its current task has been brought to the front",
    }
)


# LaunchState only as seen (API 36): COLD, WARM, HOT, UNKNOWN (0); another
# value is unknown output until it is seen. ThisTime only on API 24.
_START_INFO = re.compile(
    r"(LaunchState): (?:COLD|WARM|HOT|UNKNOWN \(0\))"
    r"|(ThisTime|TotalTime|WaitTime): -?[0-9]{1,10}"
)


_START_OK: Final = "Status: ok"


SYSTEM_PACKAGE: Final = "android"


_START_DONE: Final = "Complete"


_START_ACTIVITY: Final = "Activity:"


# C0 and C1 control characters; a line keeps none after its CR LF end is cut.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


_FOREGROUND_LIMIT: Final = 1000


def _check_launch_timeout(timeout: object) -> float:
    """Return `timeout` as a float, read through the base type's own method.

    An int or float subclass cannot change the value checked here: the
    number is converted first, with ``int.__float__`` or ``float.__float__``,
    and the converted value is the one checked and used.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        msg = f"timeout must be a number, not {type(timeout).__name__}"
        raise TypeError(msg)
    # The real type decides, not a `__class__` that claims another one.
    try:
        if isinstance(timeout, int) and not issubclass(type(timeout), float):
            value = int.__float__(timeout)
        elif isinstance(timeout, float):
            value = float.__float__(timeout)
        else:  # an int that is also a float cannot be made; kept for the types
            value = float("nan")
    except OverflowError:
        value = float("inf")
    if not 0 <= value <= MAX_LAUNCH_TIMEOUT:  # also false for NaN
        msg = f"timeout must be from 0 to {MAX_LAUNCH_TIMEOUT:g} seconds, not {value!r}"
        raise ValueError(msg)
    return value


def _is_component(text: str, package: str) -> bool:
    """Whether `text` is ``PACKAGE/CLASS`` with exactly `package` before the
    slash and a class name of the accepted shape and length after it."""
    owner, slash, name = text.partition("/")
    return (
        slash == "/"
        and owner == package
        and len(name) <= _MAX_CLASS_NAME
        and _CLASS_NAME.fullmatch(name) is not None
    )


def _launcher_component(output: str, package: str) -> str | None:
    """Read the component from ``cmd package resolve-activity --brief ...;
    echo uiprobe-exit:$?`` output.

    Accepted: zero or more lines of the `_RESOLVE_INFO` form, then exactly one
    line ``PACKAGE/CLASS`` of `package`, then the line ``uiprobe-exit:0``,
    last (empty lines and one CR at each line end are ignored). Anything
    else is None: ``No activity found``, another exit status, and output that
    ends early (no marker line, so a component cut off at the end of the
    output is never taken).
    """
    lines = [line for line in _lines(output) if line]
    if len(lines) < 2 or lines[-1] != f"{EXIT_MARKER}0":
        return None
    *info, last = lines[:-1]
    if not all(_RESOLVE_INFO.fullmatch(line) for line in info):
        return None
    return last if _is_component(last, package) else None


def _home_package(output: str) -> str | None:
    """Read the home app's package from ``cmd package resolve-activity --brief
    -a android.intent.action.MAIN -c android.intent.category.HOME; echo
    uiprobe-exit:$?`` output.

    The package is the part before the slash of the last line before the
    marker line ``uiprobe-exit:0`` (which must be the last line; empty lines
    and one CR at each line end are ignored). The lines before it are not
    looked at, so a device that prints other information lines still gives
    its home app. None when the marker is missing or not 0, the last line is
    not ``PACKAGE/CLASS`` with a valid package name, or the package has
    fewer than two segments (``android`` is the answer when the system asks
    which home app to use: that names no home app).
    """
    lines = [line for line in _lines(output) if line]
    if len(lines) < 2 or lines[-1] != f"{EXIT_MARKER}0":
        return None
    owner, slash, name = lines[-2].partition("/")
    valid = (
        slash == "/"
        and len(owner) <= MAX_PACKAGE_LENGTH
        and PACKAGE_PATTERN.fullmatch(owner) is not None
        and len(name) <= _MAX_CLASS_NAME
        and _CLASS_NAME.fullmatch(name) is not None
    )
    return owner if valid else None


def _spellings(component: str) -> frozenset[str]:
    """The short (``.Name``) and full spellings of ``PACKAGE/CLASS``."""
    package, _, name = component.partition("/")
    if name.startswith("."):
        return frozenset({component, f"{package}/{package}{name}"})
    if name.startswith(package + "."):
        return frozenset({component, f"{package}/{name[len(package) :]}"})
    return frozenset({component})


class _StartFailure(NamedTuple):
    """Why ``am start -W`` output is not a success.

    ``line`` is the line to show (None: the first line that is not blank),
    ``exit_status`` the status when it was known and not 0, and ``other``
    the component of another package that ``Activity:`` named.
    """

    reason: str
    problem: str
    line: str | None = None
    exit_status: int | None = None
    other: str | None = None


def _start_failure(output: str, package: str, activity: str) -> _StartFailure | None:
    """Judge ``am start -W ... -n ACTIVITY; echo uiprobe-exit:$?`` output.

    None is success: no line holds a control character (after one CR at its
    end is cut), no line starts with ``Error``, the marker line is last with
    exit status 0, and the other lines are, each kind at most once: exactly
    one ``Starting: Intent { act=android.intent.action.MAIN
    cat=[android.intent.category.LAUNCHER] cmp=ACTIVITY }`` (in the short or
    the full spelling of `activity`), one of the two captured ``Warning:``
    lines, exactly one ``Status: ok``, exactly one ``Activity: PACKAGE/CLASS``
    of `package` (any class: a launcher activity may hand over to another
    one), ``LaunchState:``, ``ThisTime:``, ``TotalTime:``, ``WaitTime:`` and
    ``Complete``. Empty lines are ignored. API 24 prints its ``Error`` lines
    with exit status 0, so the status alone proves nothing. Output that is a
    success in every other way but names an activity of another package gives
    the reason ``other_activity``.
    """
    lines = [line for line in _lines(output) if line]
    control = next((line for line in lines if _CONTROL.search(line)), None)
    if control is not None:
        return _StartFailure(
            "unexpected_output", "printed a control character", control
        )
    marker = lines[-1] if lines and lines[-1].startswith(EXIT_MARKER) else None
    others = lines[:-1] if marker is not None else lines
    value = None if marker is None else marker[len(EXIT_MARKER) :]
    status = int(value) if value is not None and _EXIT_VALUE.fullmatch(value) else None
    failed = None if status in (None, 0) else status
    errors = [line for line in others if line.startswith("Error")]
    # API 24 and 36 print ``Error type 3`` and then the line that says why.
    error = next((line for line in errors if line.startswith("Error:")), None)
    error = error or next(iter(errors), None)
    if error is not None:
        return _StartFailure("start_failed", "reported an error", error, failed)
    if failed is not None:
        return _StartFailure(
            "start_failed", f"ended with exit status {failed}", None, failed
        )
    if status is None:
        return _StartFailure("unexpected_output", "reported no exit status")
    return _start_report_failure(others, package, activity)


def _start_line_kind(line: str, echoes: frozenset[str]) -> str | None:
    """The kind of one line of a start's report, or None when unknown."""
    if line in echoes:
        return "Starting"
    if line in _START_WARNINGS:
        return "Warning"
    if line in (_START_OK, _START_DONE):
        return line
    if line.startswith(_START_ACTIVITY):
        return _START_ACTIVITY
    info = _START_INFO.fullmatch(line)
    return None if info is None else info.group(1) or info.group(2)


def _start_report_failure(
    lines: list[str], package: str, activity: str
) -> _StartFailure | None:
    """Judge the lines of a start that reported exit status 0 and no error."""
    echoes = frozenset(_START_ECHO.format(c) for c in _spellings(activity))
    kinds: list[str] = []
    for line in lines:
        kind = _start_line_kind(line, echoes)
        if kind is None:
            what = (
                "printed a LaunchState value it does not know"
                if line.startswith("LaunchState:")
                else "printed an unknown line"
            )
            return _StartFailure("unexpected_output", what, line)
        if kind in kinds:
            return _StartFailure("unexpected_output", "printed a line twice", line)
        kinds.append(kind)
    missing = [k for k in ("Starting", _START_OK, _START_ACTIVITY) if k not in kinds]
    if missing:
        what = "the start line" if missing[0] == "Starting" else f"'{missing[0]}'"
        return _StartFailure("unexpected_output", f"did not print {what}")
    named = next(line for line in lines if line.startswith(_START_ACTIVITY))
    component = named.removeprefix(_START_ACTIVITY + " ")
    if component != named and _is_component(component, package):
        return None
    owner = component.partition("/")[0]
    # The system owner ``android`` (its chooser,
    # android/com.android.internal.app.ResolverActivity) has no dot, unlike an
    # app's package; it is accepted here, for the activity reported only.
    if (
        component != named
        and owner != package
        and len(owner) <= MAX_PACKAGE_LENGTH
        and (owner == SYSTEM_PACKAGE or PACKAGE_PATTERN.fullmatch(owner))
        and _is_component(component, owner)
    ):
        return _StartFailure(
            "other_activity", "named an activity of another app", named, None, component
        )
    return _StartFailure("unexpected_output", f"named no activity of {package}", named)


def _shown_foreground(name: str) -> str:
    """Return `name` escaped; past 1000 characters it is cut and ends in ``...``.

    A name cut this way is never equal to a package name, which has at most
    255 characters.
    """
    if len(name) <= _FOREGROUND_LIMIT:
        return _escaped(name, _FOREGROUND_LIMIT)
    return _escaped(name, _FOREGROUND_LIMIT) + "..."
