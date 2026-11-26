"""The packages a pressing crawl may also press in (``[rules] also_target``).

A listed package counts as the target app for the judging of its screens (see
`uiprobe.guards.judge`) and for the engine, with every guard that applies to
the app itself. This module holds what is refused before anything is sent: the
system UI, the home app, the target app itself, and a name that is not a
package name (the static rules live in `uiprobe.guards.policy`, so that
`Policy` and `CrawlEngine` can refuse them too). The home app is the device's
own answer to `HOME_COMMAND` (read once at the start, only when the list is
not empty; if it cannot be read the run is refused) and the fixed list
`FIXED_HOME_APPS`. Permission screens are never pressed, listed or not (that
guard is in `uiprobe.guards.judge` and does not look at this list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from uiprobe.guards.policy import (
    FIXED_HOME_APPS,
    SYSTEM_UI,
    AlsoTargetError,
    check_also_target,
)

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
