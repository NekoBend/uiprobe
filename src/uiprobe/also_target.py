"""The packages a pressing crawl may also press in (``[rules] also_target``).

A listed package counts as the target app for the judging of its screens (see
`uiprobe.guards.judge`) and for the engine, with every guard that applies to
the app itself. This module holds what is refused before anything is sent: the
system UI, the home app, the target app itself, and a name that is not a
package name. The home app is the device's own answer to
`HOME_COMMAND` (read once at the start, only when the list is not empty; if it cannot be
read the run is refused) and the fixed list
`FIXED_HOME_APPS`. Permission screens are never pressed, listed or not (that
guard is in `uiprobe.guards.judge` and does not look at this list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from uiprobe.crawl_preview import check_package

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "FIXED_HOME_APPS",
    "HOME_COMMAND",
    "HOME_UNREADABLE",
    "SYSTEM_UI",
    "AlsoTargetError",
    "check_also_target",
    "check_home",
    "not_installed_error",
]

# The package of the lock screen and the notification shade.
SYSTEM_UI: Final = "com.android.systemui"
# Home apps (launchers) that are refused without asking the device: AOSP's,
# Google's Pixel launcher and Samsung's One UI Home.
FIXED_HOME_APPS: Final = (
    "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
    "com.sec.android.app.launcher",
)
HOME_UNREADABLE: Final = (
    "also_target: could not read this device's home app, which also_target"
    " needs in order to refuse it; nothing was pressed"
)
# The read-only command that names the device's home activity (``pkg/.Activity``
# on its last line); sent through the shell path of `Device`, once at the start
# of a run that lists a package.
HOME_COMMAND: Final = (
    "cmd package resolve-activity --brief"
    " -a android.intent.action.MAIN -c android.intent.category.HOME"
)


class AlsoTargetError(ValueError):
    """A package listed in ``also_target`` is refused (nothing was sent)."""


def check_also_target(target: str, listed: Iterable[str]) -> tuple[str, ...]:
    """Check the list before any device is contacted; return it as a tuple.

    Raises:
        AlsoTargetError: An entry is not a valid package name, is listed twice,
            is `target` itself, is the system UI or is in `FIXED_HOME_APPS`.
    """
    if isinstance(listed, str):
        msg = "also_target must be a list of package names, not one string"
        raise AlsoTargetError(msg)
    found: list[str] = []
    for item in listed:
        try:
            package = check_package(item)
        except (TypeError, ValueError) as error:
            raise AlsoTargetError(f"also_target: {error}") from None
        if package in found:
            msg = f"also_target lists {package} more than once"
            raise AlsoTargetError(msg)
        if package == target:
            msg = (
                f"also_target lists {package}, which is the target app itself;"
                " remove it from the list"
            )
            raise AlsoTargetError(msg)
        if package == SYSTEM_UI:
            msg = (
                f"also_target cannot list {package}: it is the system UI (lock"
                " screen, notification shade); nothing was pressed"
            )
            raise AlsoTargetError(msg)
        if package in FIXED_HOME_APPS:
            msg = (
                f"also_target cannot list {package}: it is a home app"
                " (launcher); nothing was pressed"
            )
            raise AlsoTargetError(msg)
        found.append(package)
    return tuple(found)


def check_home(listed: Iterable[str], home: str | None) -> None:
    """Refuse the run when the device's home app is listed or unknown.

    `home` is the package `Device.home_package` read; None means the read
    failed or did not name exactly one component, and the run is refused: the
    home app is what this check needs in order to refuse it, so an unreadable
    one fails closed (the fixed list of `check_also_target` stays an extra
    check). The home app is read once at the start; one changed during the run
    is not noticed.

    Raises:
        AlsoTargetError: `home` is None, or a listed package is `home`.
    """
    if home is None:
        raise AlsoTargetError(HOME_UNREADABLE)
    if home in tuple(listed):
        msg = (
            f"also_target cannot list {home}: it is the home app of this"
            " device; nothing was pressed"
        )
        raise AlsoTargetError(msg)


def not_installed_error(package: str) -> AlsoTargetError:
    """The refusal for a listed package whose uid could not be read."""
    msg = (
        f"also_target lists {package}, which is not installed on this device"
        " (or its uid could not be read); nothing was pressed"
    )
    return AlsoTargetError(msg)
